"""The local adapter preserves glossary lifetime and read-only Review semantics."""

import io
import ntpath
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm
from wenyi_core.pipeline.runstore import RunStore
from wenyi_core.storage.file import FileStorage


@pytest.mark.parametrize(
    ("directory", "io_directory"),
    [
        (r"C:\state\run", r"\\?\C:\state\run"),
        (r"\\server\share\run", r"\\?\UNC\server\share\run"),
        (r"\\?\C:\state\run", r"\\?\C:\state\run"),
        (r"\\?\UNC\server\share\run", r"\\?\UNC\server\share\run"),
    ],
)
def test_windows_runstore_json_uses_valid_io_paths(monkeypatch, directory, io_directory):
    import wenyi_core.pipeline.runstore as runstore
    import wenyi_core.storage.artifacts as artifacts

    calls = []
    windows_os = SimpleNamespace(
        name="nt",
        path=ntpath,
        makedirs=lambda path, **kwargs: calls.append(("mkdir", path, kwargs)),
        replace=lambda source, target: calls.append(("replace", source, target)),
    )

    def open_json(path, mode, *, encoding):
        calls.append(("open", path, mode, encoding))
        return io.StringIO('{"target": "translation"}' if mode == "r" else "")

    monkeypatch.setattr(runstore, "os", windows_os)
    monkeypatch.setattr(artifacts, "os", windows_os)
    monkeypatch.setattr(runstore, "open", open_json, raising=False)
    path = ntpath.join(directory, "context.json")
    io_path = ntpath.join(io_directory, "context.json")
    RunStore._write_json(path, {"target": "translation"})
    assert RunStore._read_json(path) == {"target": "translation"}
    assert calls == [
        ("mkdir", io_directory, {"exist_ok": True}),
        ("open", io_path + ".tmp", "w", "utf-8"),
        ("replace", io_path + ".tmp", io_path),
        ("open", io_path, "r", "utf-8"),
    ]


@pytest.mark.parametrize("interrupted", [False, True])
def test_long_json_write_is_atomic_and_can_resume(tmp_path, monkeypatch, interrupted):
    storage = FileStorage(str(tmp_path))
    prefix = "precision/" + "a" * 100 + "/" + "b" * 100 + "/" + "c" * 50 + "/"
    key = prefix + "result.json"
    path = str(tmp_path / key)
    assert len(path + ".tmp") > 260
    RunStore._write_json(path, {"target": "original"})

    if interrupted:

        def interrupt(*_):
            raise OSError("interrupted before replace")

        with monkeypatch.context() as patch:
            patch.setattr("wenyi_core.pipeline.runstore.os.replace", interrupt)
            with pytest.raises(OSError, match="interrupted before replace"):
                RunStore._write_json(path, {"target": "partial"})
        assert RunStore._read_json(path) == {"target": "original"}

    RunStore._write_json(path, {"target": "complete"})
    assert RunStore._read_json(path) == {"target": "complete"}
    assert storage.read_artifact(key) == {"target": "complete"}
    assert storage.list_artifacts(prefix) == [key]


@pytest.mark.parametrize("interrupted", [False, True])
def test_run_lock_closes_glossary_and_allows_reuse(tmp_path, interrupted):
    storage = FileStorage(str(tmp_path))
    term = GlossaryTerm(source="Alice", target="爱丽丝")
    with pytest.raises(RuntimeError, match="interrupted") if interrupted else nullcontext():
        with storage.lock():
            storage.upsert_term(term)
            connection = storage._g.conn
            if interrupted:
                raise RuntimeError("interrupted")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with storage.lock():
        assert storage.get_term(term.source) == term


def test_reading_terms_keeps_formal_glossary_and_wal_unchanged(tmp_path):
    storage = FileStorage(str(tmp_path))
    writer = GlossaryStore(storage.glossary_path)
    try:
        writer.conn.execute("PRAGMA wal_autocheckpoint=0")
        term = GlossaryTerm(source="Alice", target="爱丽丝")
        writer.upsert_term(term)

        def snapshot():
            return {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in tmp_path.glob("glossary.db*")
            }

        before = snapshot()
        assert storage.all_terms() == [term]
        assert storage._glossary is None
        assert snapshot() == before
    finally:
        storage.close()
        writer.close()


def test_reading_missing_glossary_does_not_create_database(tmp_path):
    storage = FileStorage(str(tmp_path))
    try:
        assert storage.all_terms() == []
        assert not Path(storage.glossary_path).exists()
    finally:
        storage.close()
