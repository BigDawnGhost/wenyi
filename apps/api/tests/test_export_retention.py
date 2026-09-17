"""Bounded export storage, concurrent publication and download ownership."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_project_routes import api, new_project  # noqa: F401
from test_storage_pg_integration import pg_pool  # noqa: F401
from wenyi_api import dal
from wenyi_api.db import get_pool
from wenyi_api.export_retention import open_export, publish_export


def pending_export(pid, data_dir, fmt="txt"):
    eid = dal.create_export(pid, fmt, {})
    path = Path(data_dir) / pid / "exports" / str(eid) / f"translation.{fmt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"export {eid}", encoding="utf-8")
    return eid, path


def publish(pid, item, data_dir):
    eid, path = item
    publish_export(get_pool(), pid, eid, str(path), data_dir=str(data_dir))


def completed_ids(pid):
    with get_pool().connection() as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT id FROM exports WHERE project_id=%s AND status='done'", (pid,)
            ).fetchall()
        }


def test_retains_five_successes_in_completion_order_and_isolates_projects(api, tmp_path):  # noqa: F811
    client, _ = api
    data_dir = tmp_path / "data"
    pid, other = new_project(api), new_project(api)
    source = data_dir / pid / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("source stays untouched", encoding="utf-8")
    other_file = pending_export(other, data_dir)
    publish(other, other_file, data_dir)
    items = [pending_export(pid, data_dir) for _ in range(7)]
    # The oldest request finishes last and must remain downloadable.
    order = [*items[1:], items[0]]
    for item in order:
        publish(pid, item, data_dir)
    assert completed_ids(pid) == {eid for eid, _ in order[-5:]}
    assert sum(path.exists() for _, path in items) == 5
    assert other_file[1].is_file() and source.read_text() == "source stays untouched"
    assert client.get(f"/projects/{pid}/exports/{order[0][0]}/download").status_code == 404
    response = client.get(f"/projects/{pid}/exports/{items[0][0]}/download")
    assert response.status_code == 200 and response.text == f"export {items[0][0]}"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert client.get(f"/projects/{other}/exports/{items[0][0]}/download").status_code == 404
    rows = client.get(f"/projects/{pid}/exports").json()
    assert [row["id"] for row in rows] == [eid for eid, _ in reversed(order[-5:])]
    failed = dal.create_export(pid, "txt", {})
    dal.set_export_status(failed, "error", error="renderer unavailable")
    dal.create_export(pid, "txt", {})
    assert len(client.get(f"/projects/{pid}/exports").json()) == 5
    assert completed_ids(pid) == {eid for eid, _ in order[-5:]}


def test_concurrent_publications_keep_five_and_open_download_survives_eviction(api, tmp_path):  # noqa: F811
    pid, data_dir = new_project(api), tmp_path / "data"
    oldest = pending_export(pid, data_dir)
    publish(pid, oldest, data_dir)
    stream, _ = open_export(get_pool(), pid, oldest[0], data_dir=str(data_dir))
    try:
        items = [pending_export(pid, data_dir) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda item: publish(pid, item, data_dir), items))
        assert len(completed_ids(pid)) == 5
        assert sum(path.exists() for _, path in items) == 5
        assert not oldest[1].exists()
        assert stream.read().decode() == f"export {oldest[0]}"
    finally:
        stream.close()


def test_expired_html_assets_removed_and_cleanup_failure_retried(api, tmp_path, monkeypatch):  # noqa: F811
    pid, data_dir = new_project(api), tmp_path / "data"
    oldest = pending_export(pid, data_dir, "html")
    assets = oldest[1].with_name("translation.assets")
    assets.mkdir()
    (assets / "image.png").write_bytes(b"test image")
    publish(pid, oldest, data_dir)
    original = Path.unlink

    def unavailable(file, *args, **kwargs):
        if file == oldest[1]:
            raise PermissionError("file temporarily busy")
        return original(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unavailable)
        for _ in range(5):
            publish(pid, pending_export(pid, data_dir), data_dir)
    assert oldest[1].exists()
    assert len(completed_ids(pid)) == 6
    publish(pid, pending_export(pid, data_dir), data_dir)
    assert not oldest[1].exists() and not assets.exists()
    assert len(completed_ids(pid)) == 5


@pytest.mark.parametrize("path_kind", ["source", "other_project", "symlink"])
def test_retention_and_downloads_refuse_non_export_files(api, tmp_path, path_kind):  # noqa: F811
    client, _ = api
    pid, other, data_dir = new_project(api), new_project(api), tmp_path / "data"
    source = data_dir / pid / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("original", encoding="utf-8")
    foreign = pending_export(other, data_dir)[1]
    owned = pending_export(pid, data_dir)
    link = owned[1].with_name("link.txt")
    link.symlink_to(source)
    bad = {"source": source, "other_project": foreign, "symlink": link}[path_kind]
    with pytest.raises(ValueError, match="export directory"):
        publish(pid, (owned[0], bad), data_dir)
    dal.set_export_status(owned[0], "done", path=str(bad.relative_to(data_dir)), size=8)
    for _ in range(5):
        publish(pid, pending_export(pid, data_dir), data_dir)
    assert source.read_text() == "original" and foreign.is_file()
    assert client.get(f"/projects/{pid}/exports/{owned[0]}/download").status_code == 404


def test_publication_cannot_update_another_projects_record(api, tmp_path):  # noqa: F811
    pid, other, data_dir = new_project(api), new_project(api), tmp_path / "data"
    eid, _ = pending_export(other, data_dir)
    _, path = pending_export(pid, data_dir)
    with pytest.raises(ValueError, match="does not belong"):
        publish(pid, (eid, path), data_dir)
    assert not completed_ids(other)
