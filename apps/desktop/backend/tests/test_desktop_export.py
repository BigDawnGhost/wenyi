"""Native save planning and bounded, project-owned export transport."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wenyi_backend import dal
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_desktop import main


@pytest.fixture
def desktop_exports(desktop_context):
    backend = desktop_context.repository
    pid = dal.create_project("Example", "en", "zh", {})
    source = backend.project_dir(pid) / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("A paragraph.", encoding="utf-8")
    store = backend.storage_for(pid)
    store.init_from_document(
        Document(
            title="Example",
            source_path=str(source),
            fmt="txt",
            source_lang="en",
            target_lang="zh",
            chapters=[Chapter(index=0, title="One", segments=[Segment(index=0, source="Text")])],
        )
    )
    dal.set_project_source(
        pid,
        source_path=str(source),
        book_title="Example",
        source_sha256=store.load_manifest()["source_sha256"],
        fmt="txt",
        source_meta={"original_filename": "Example book.txt"},
    )
    store.close()
    client = TestClient(main.create_app(context=desktop_context))
    try:
        yield client, backend, pid
    finally:
        client.close()


@pytest.mark.parametrize("fmt", ["txt", "markdown", "html", "epub", "docx"])
@pytest.mark.parametrize("bilingual", [False, True])
def test_plan_is_side_effect_free_and_matches_export_filename(desktop_exports, fmt, bilingual):
    client, backend, pid = desktop_exports
    before = (backend.list_exports(pid), backend.all_jobs())
    response = client.post(
        f"/desktop/projects/{pid}/exports/plan", json={"format": fmt, "bilingual": bilingual}
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    extension = {"markdown": "md", "html": "html.zip"}.get(fmt, fmt)
    assert plan["filename"] == f"Example book.zh{'-bi' if bilingual else ''}.{extension}"
    assert plan["request"]["format"] == fmt
    assert plan["request"]["bilingual"] is bilingual
    assert plan["protected_roots"] == [str(backend.workspace.resolve())]
    assert (backend.list_exports(pid), backend.all_jobs()) == before


def publish(backend, pid, fmt, content):
    export_id = backend.create_export(pid, fmt, {})
    root = backend.project_dir(pid) / "exports" / str(export_id)
    root.mkdir(parents=True)
    file = root / f"Example.zh.{fmt}"
    file.write_bytes(content)
    backend.publish_export(pid, export_id, str(file))
    return export_id, file


def test_html_save_contains_only_published_document_and_owned_assets(desktop_exports):
    client, backend, pid = desktop_exports
    export_id, file = publish(backend, pid, "html", b'<img src="Example.zh.assets/image.png">')
    assets = file.with_suffix(".assets")
    assets.mkdir()
    (assets / "image.png").write_bytes(b"image payload")
    (file.parent / "not-owned.txt").write_bytes(b"not exported")
    url = f"/desktop/projects/{pid}/exports/{export_id}"
    plan = client.get(url + "/plan")
    assert plan.status_code == 200
    assert plan.json()["filename"] == "Example.zh.html.zip"
    assert plan.json()["label"] == "HTML + assets (ZIP)"
    content = client.get(url + "/content")
    assert content.status_code == 200, content.text
    assert len(content.content) == int(content.headers["content-length"])
    with zipfile.ZipFile(io.BytesIO(content.content)) as bundle:
        assert bundle.namelist() == ["Example.zh.html", "Example.zh.assets/image.png"]
        assert bundle.read("Example.zh.assets/image.png") == b"image payload"
        assert b"Example.zh.assets/image.png" in bundle.read("Example.zh.html")
    # Web downloads remain the original HTML, not a silently substituted archive.
    assert client.get(f"/projects/{pid}/exports/{export_id}/download").content == file.read_bytes()


@pytest.mark.parametrize("linked_directory", [False, True])
def test_html_bundle_rejects_links_outside_export(desktop_exports, tmp_path, linked_directory):
    client, backend, pid = desktop_exports
    export_id, file = publish(backend, pid, "html", b"document")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"must not be exported")
    assets = file.with_suffix(".assets")
    if linked_directory:
        assets.symlink_to(outside, target_is_directory=True)
    else:
        assets.mkdir()
        (assets / "secret.txt").symlink_to(outside / "secret.txt")
    response = client.get(f"/desktop/projects/{pid}/exports/{export_id}/content")
    assert response.status_code == 409
    assert b"must not be exported" not in response.content


def test_native_content_is_project_scoped_and_requires_published_status(desktop_exports):
    client, backend, pid = desktop_exports
    export_id, _ = publish(backend, pid, "txt", b"translation")
    other = dal.create_project("Other", "en", "zh", {})
    for suffix in ("plan", "content"):
        assert (
            client.get(f"/desktop/projects/{other}/exports/{export_id}/{suffix}").status_code == 404
        )
    pending = backend.create_export(pid, "txt", {})
    assert client.get(f"/desktop/projects/{pid}/exports/{pending}/content").status_code == 404
    response = client.get(f"/desktop/projects/{pid}/exports/{export_id}/content")
    assert response.content == b"translation"
    assert response.headers["content-length"] == "11"


def test_bundle_rejects_hard_linked_private_file(tmp_path):
    from wenyi_desktop.local_export_files import html_bundle

    file = tmp_path / "book.html"
    file.write_bytes(b"document")
    assets = tmp_path / "book.assets"
    assets.mkdir()
    private = tmp_path / "private.txt"
    private.write_bytes(b"private")
    os.link(private, assets / "alias.txt")
    with pytest.raises(ValueError, match="multiply linked"):
        html_bundle(file)


def test_archiving_does_not_hold_catalog_transaction(desktop_exports, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from wenyi_desktop import local_export_files

    client, backend, pid = desktop_exports
    export_id, _ = publish(backend, pid, "html", b"document")
    original = local_export_files._copy_file

    def copy(*args, **kwargs):
        # A separate thread/connection must be able to commit while compression runs.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(backend.set_project_status, pid, "done").result(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(local_export_files, "_copy_file", copy)
    assert client.get(f"/desktop/projects/{pid}/exports/{export_id}/content").status_code == 200


def test_bundle_handles_directory_swap_without_following_link(tmp_path, monkeypatch):
    from wenyi_desktop.local_export_files import html_bundle

    root = tmp_path / "export"
    root.mkdir()
    file = root / "book.html"
    file.write_bytes(b"document")
    assets = root / "book.assets"
    assets.mkdir()
    (assets / "image.png").write_bytes(b"owned image")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "image.png").write_bytes(b"private")
    if os.name == "nt":
        pytest.skip(
            "POSIX descriptor-relative race injection; Windows uses non-delete-sharing handles"
        )
    original = os.open
    swapped = False

    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path).name == "book.assets" and not swapped:
            swapped = True
            assets.rename(root / "retired.assets")
            assets.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises((OSError, ValueError)):
        html_bundle(file)
    assert swapped


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("fmt", ["txt", "html"])
def test_disconnect_closes_export_stream(desktop_exports, monkeypatch, native, fmt):
    import asyncio

    from starlette.requests import ClientDisconnect
    from wenyi_backend.routers import export
    from wenyi_desktop.routers import desktop_export

    _, backend, pid = desktop_exports
    export_id, output = publish(backend, pid, fmt, b"translation")
    opened = []
    original = backend.open_export

    def capture(*args, **kwargs):
        stream, file = original(*args, **kwargs)
        opened.append(stream)
        return stream, file

    monkeypatch.setattr(backend, "open_export", capture)
    response = (desktop_export.save_content if native else export.download_export)(pid, export_id)
    for _ in range(5):
        publish(backend, pid, fmt, b"newer")
    assert all(row["id"] != export_id for row in backend.list_exports(pid))
    if fmt == "txt":
        assert output.exists()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("Client disconnected")

    async def disconnected():
        with pytest.raises(ClientDisconnect):
            await response(
                {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}}, receive, send
            )
        assert opened[0].closed
        assert not output.exists()
        assert backend._get("exports", export_id) is None

    asyncio.run(disconnected())


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids these characters in directory names")
def test_posix_workspace_ancestors_are_not_zip_entry_names(tmp_path):
    from wenyi_desktop.local_export_files import html_bundle, open_regular

    root = tmp_path / "wenyi:desktop\\workspace"
    root.mkdir()
    file = root / "book.html"
    file.write_bytes(b"document")
    with open_regular(file) as stream:
        assert stream.read() == b"document"
    with html_bundle(file) as stream, zipfile.ZipFile(stream) as archive:
        assert archive.read("book.html") == b"document"


@pytest.mark.parametrize("missing", ["new", "removed"])
def test_plan_and_project_reads_never_create_missing_domain_state(desktop_context, missing):
    import shutil

    backend = desktop_context.repository
    pid = backend.create_project("Missing state", "en", "zh", {})
    root = backend.project_dir(pid)
    if missing == "removed":
        store = backend.storage_for(pid)
        store.write_artifact("preview.json", {"title": "Previously parsed"})
        store.close()
        shutil.rmtree(root)
    assert not root.exists()
    before = (backend.list_exports(pid), backend.all_jobs())
    client = TestClient(main.create_app(context=desktop_context))
    try:
        project = client.get(f"/projects/{pid}")
        assert project.status_code == 200
        assert project.json()["initialized"] is False
        assert client.get("/projects").status_code == 200
        for route, status in (
            ("chapters", 200),
            ("chapters/0", 404),
            ("review/0", 404),
            ("preview", 409),
        ):
            assert client.get(f"/projects/{pid}/{route}").status_code == status
        response = client.post(f"/desktop/projects/{pid}/exports/plan", json={"format": "txt"})
        assert response.status_code == 409
        assert not root.exists()
        assert (backend.list_exports(pid), backend.all_jobs()) == before
    finally:
        client.close()
