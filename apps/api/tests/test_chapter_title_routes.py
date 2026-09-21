"""Chapter title edits persist atomically and reach export without changing body text."""

from __future__ import annotations

import zipfile

import pytest
import test_domain_routes_integration as domain_tests
from bs4 import BeautifulSoup
from tests.sample_data import write_nested_toc_epub
from wenyi_api import dal
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.pipeline.title_translation import plan_titles

pg_pool = domain_tests.pg_pool
pg_storage = domain_tests.pg_storage
domain_client = domain_tests.domain_client


def test_title_save_updates_only_linked_toc_nodes_and_survives_reload(domain_client, tmp_path):
    client, storage, _ = domain_client
    domain_tests.initialize(storage, tmp_path)
    manifest = storage.load_manifest()
    primary = {
        "entry_id": "toc-a",
        "toc_path": "nav.xhtml",
        "node_index": 0,
        "title": "Chapter",
        "target_key": "body.xhtml#chapter",
        "raw_href": "body.xhtml#chapter",
    }
    duplicate = {**primary, "entry_id": "ncx-a", "toc_path": "toc.ncx"}
    unrelated = {**primary, "entry_id": "toc-b", "target_key": "body.xhtml#other"}
    shortened = {**primary, "entry_id": "toc-c", "title": "Ch."}
    grouping = {"entry_id": "toc-group", "title": "Chapter"}
    manifest["meta"]["toc_entries"] = [primary, duplicate, unrelated, shortened, grouping]
    storage.save_manifest(manifest)
    original = storage.load_chapter(0)
    url = f"/projects/{storage.project_id}/chapters/0/title"
    result = client.put(
        url, json={"title_translated": "  人工目录  ", "expected_title_translated": None}
    )
    assert result.status_code == 200
    assert result.json() == {"index": 0, "title_translated": "人工目录"}
    saved = storage.load_manifest()
    assert saved["chapters"][0]["title_translated"] == "人工目录"
    assert saved["chapters"][0]["title"] == "Chapter"
    assert [entry.get("title_translated") for entry in saved["meta"]["toc_entries"]] == [
        "人工目录",
        "人工目录",
        None,
        None,
        None,
    ]
    assert storage.load_chapter(0).segments == original.segments
    assert storage.load_chapter(0).meta["title_translated"] == "人工目录"
    root = f"/projects/{storage.project_id}"
    assert client.get(root + "/chapters").json()[0]["title_translated"] == "人工目录"
    assert client.get(root + "/review/0").json()["title_translated"] == "人工目录"
    assert (
        client.put(
            url, json={"title_translated": "Lost edit", "expected_title_translated": None}
        ).status_code
        == 409
    )
    assert storage.load_manifest() == saved
    assert (
        client.put(
            url, json={"title_translated": "人工目录", "expected_title_translated": "人工目录"}
        ).status_code
        == 200
    )
    events = storage.list_events(event_type="chapter_title_edited")
    assert len(events) == 1
    assert events[0]["before"] is None and events[0]["after"] == "人工目录"
    plan = plan_titles(saved, {0: storage.load_chapter(0)})
    assert all(item.record.get("entry_id") not in {"toc-a", "ncx-a"} for item in plan.pending)


@pytest.mark.parametrize("toc_kind", ["nav", "ncx"])
@pytest.mark.parametrize("bilingual", [False, True])
def test_saved_directory_title_is_used_by_epub_exports(
    domain_client, tmp_path, toc_kind, bilingual
):
    client, storage, _ = domain_client
    source = tmp_path / "book.epub"
    write_nested_toc_epub(str(source), toc_kind=toc_kind, nav_in_spine=toc_kind == "nav")
    document = load_document(str(source), "en", "zh")
    storage.init_from_document(document)
    manifest = storage.load_manifest()
    ci = next(row["index"] for row in manifest["chapters"] if row.get("toc_entry_id"))
    original = storage.load_chapter(ci)
    response = client.put(
        f"/projects/{storage.project_id}/chapters/{ci}/title",
        json={"title_translated": "新的目录标题", "expected_title_translated": None},
    )
    assert response.status_code == 200
    snapshot = storage.create_export_snapshot(actual_sha256=manifest["source_sha256"])
    out = tmp_path / "export.epub"
    assemble(
        snapshot,
        str(source),
        out_path=str(out),
        out_format="epub",
        bilingual=bilingual,
        about_page=False,
    )
    path = "OEBPS/nav.xhtml" if toc_kind == "nav" else "OEBPS/toc.ncx"
    with zipfile.ZipFile(out) as archive:
        toc = BeautifulSoup(archive.read(path), "html.parser" if toc_kind == "nav" else "xml")
    assert "新的目录标题" in toc.get_text()
    assert [
        node.get("href" if toc_kind == "nav" else "src")
        for node in toc.find_all("a" if toc_kind == "nav" else "content")
    ] == [entry["raw_href"] for entry in manifest["meta"]["toc_entries"]]
    assert storage.load_chapter(ci).segments == original.segments


@pytest.mark.parametrize("status", ["translating", "reviewing", "pausing", "preparing"])
def test_title_save_is_blocked_while_a_task_runs(domain_client, tmp_path, status):
    client, storage, _ = domain_client
    domain_tests.initialize(storage, tmp_path)
    dal.set_project_status(storage.project_id, status)
    before = storage.load_manifest()
    result = client.put(
        f"/projects/{storage.project_id}/chapters/0/title",
        json={"title_translated": "Blocked", "expected_title_translated": None},
    )
    assert result.status_code == 409
    assert storage.load_manifest() == before


def test_title_save_validates_input_and_project_identity(domain_client, tmp_path):
    client, storage, _ = domain_client
    url = f"/projects/{storage.project_id}/chapters/0/title"
    body = {"title_translated": "Title", "expected_title_translated": None}
    assert client.put(url, json=body).status_code == 409
    domain_tests.initialize(storage, tmp_path)
    assert client.put(url.replace("/0/", "/99/"), json=body).status_code == 404
    assert client.put(url.replace(storage.project_id, "missing"), json=body).status_code == 404
    for invalid in (
        {},
        {"title_translated": "Title"},
        {**body, "title_translated": "  "},
        {**body, "title": "Change original"},
    ):
        assert client.put(url, json=invalid).status_code == 422
    assert client.put(url, json=body).status_code == 200
    dal.set_project_source(
        storage.project_id,
        source_path="unused.srt",
        book_title="Subtitles",
        source_sha256="a" * 64,
        fmt="srt",
    )
    assert client.put(url, json={**body, "expected_title_translated": "Title"}).status_code == 422


def test_title_and_event_roll_back_together(domain_client, tmp_path, monkeypatch):
    client, storage, _ = domain_client
    domain_tests.initialize(storage, tmp_path)
    before = storage.load_manifest()

    def fail(*args, **kwargs):
        raise RuntimeError("Event write failed")

    monkeypatch.setattr(storage, "log_event", fail)
    with pytest.raises(RuntimeError, match="Event write failed"):
        client.put(
            f"/projects/{storage.project_id}/chapters/0/title",
            json={"title_translated": "Rollback", "expected_title_translated": None},
        )
    assert storage.load_manifest() == before
