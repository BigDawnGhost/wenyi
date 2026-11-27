"""The local adapter preserves glossary lifetime and read-only Review semantics."""

import json
import ntpath
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from wenyi_core.atomicio import publish_target
from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm
from wenyi_core.llm.usage import UsageSample, UsageTracker
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.storage.chapter_publication import JOURNAL_NAME
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.run_store import STATUS_DONE, STATUS_PENDING, RunStore, slugify


class _RecordingText:
    """A text file standing in for one opened through the Windows seam."""

    def __init__(self, payload: str):
        self._payload = payload

    def write(self, text: str) -> None:
        self._payload += text

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        return 0

    def read(self) -> str:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        return None

    def __iter__(self):
        return iter(self._payload.splitlines(keepends=True))


@pytest.mark.parametrize("marker", [None, "{invalid", "{}", '{"source_sha256": 1}'])
def test_initialization_without_verified_identity_discards_usage(tmp_path, marker):
    storage = FileStorage(str(tmp_path))
    tracker = UsageTracker()
    tracker.record("fast", UsageSample(10, 5, 15), stage="terminology")
    with storage.lock():
        storage.save_usage(tracker.summary())
        tracker.record("fast", UsageSample(20, 10, 30), stage="terminology")
        storage.prepare_usage_commit({"usage.json": tracker.summary()})
        if marker is not None:
            (tmp_path / ".initializing.json").write_text(marker, encoding="utf-8")
        storage.begin_initialization("a" * 64)
        storage.recover_usage()
        assert storage.load_usage() is None
        assert storage.read_artifact("usage-pending.json") is None


@pytest.mark.parametrize("published", [False, True])
def test_initialization_retry_recovers_first_usage_commit(tmp_path, published):
    storage = FileStorage(str(tmp_path))
    tracker = UsageTracker()
    tracker.record("fast", UsageSample(10, 5, 15), stage="terminology")
    ledger = tracker.summary()
    with storage.lock():
        storage.begin_initialization("a" * 64)
        storage.prepare_usage_commit({"usage.json": ledger})
        if published:
            storage.save_usage(ledger)
    # Reopen the adapter to exercise recovery without relying on in-memory state.
    storage = FileStorage(str(tmp_path))
    with storage.lock():
        for _ in range(3):
            storage.begin_initialization("a" * 64)
            assert storage.load_usage() == ledger
            assert storage.read_artifact("usage-pending.json") is None


def test_stale_initialization_marker_cleared_once_manifest_committed(tmp_path):
    storage = FileStorage(str(tmp_path))
    with storage.lock():
        storage.begin_initialization("a" * 64)
        storage.save_manifest({"source_sha256": "a" * 64, "initialized": True})
        # No finish_initialization: a crash after the manifest commit leaves the marker.
        assert os.path.isfile(storage.initialization_path)

    reopened = FileStorage(str(tmp_path))
    assert reopened.exists()
    assert not os.path.exists(reopened.initialization_path)


def test_initialization_marker_without_manifest_survives_for_recovery(tmp_path):
    storage = FileStorage(str(tmp_path))
    with storage.lock():
        storage.begin_initialization("a" * 64)
        assert os.path.isfile(storage.initialization_path)

    # A marker without a committed manifest is the recovery anchor for the next
    # initialization attempt, so reopening the store must not discard it.
    reopened = FileStorage(str(tmp_path), create=False)
    assert not reopened.exists()
    assert os.path.isfile(reopened.initialization_path)


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
    import wenyi_core.storage.atomic_write as atomic_write
    import wenyi_core.storage.io_path as io_path_module
    import wenyi_core.storage.run_store as run_store

    calls = []

    def makedirs(path, **kwargs):
        calls.append(("mkdir", path, kwargs))

    def replace(source, target):
        calls.append(("replace", source, target))

    def fsync(descriptor):
        calls.append(("fsync", descriptor))

    def remove(path):
        calls.append(("remove", path))

    windows_os = SimpleNamespace(
        name="nt",
        path=ntpath,
        makedirs=makedirs,
        replace=replace,
        fsync=fsync,
        remove=remove,
    )

    def open_json(path, mode, *, encoding, opener=None):
        assert (opener is not None) == (mode == "x")
        calls.append(("open", path, mode, encoding))
        return _RecordingText('{"target": "translation"}' if mode == "r" else "")

    monkeypatch.setattr(io_path_module, "os", windows_os)
    monkeypatch.setattr(atomic_write, "os", windows_os)
    monkeypatch.setattr(atomic_write, "open", open_json, raising=False)
    monkeypatch.setattr(run_store, "open", open_json, raising=False)
    path = ntpath.join(directory, "context.json")
    io_path = ntpath.join(io_directory, "context.json")
    RunStore._write_json(path, {"target": "translation"})
    assert RunStore._read_json(path) == {"target": "translation"}
    temporary = calls[1][1]
    assert ntpath.dirname(temporary) == io_directory
    assert re.fullmatch(r"\.wenyi-[0-9a-f]{32}\.tmp", ntpath.basename(temporary))
    assert calls == [
        ("mkdir", io_directory, {"exist_ok": True}),
        ("open", temporary, "x", "utf-8"),
        ("fsync", 0),
        ("replace", temporary, io_path),
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
            patch.setattr("wenyi_core.storage.run_store.os.replace", interrupt)
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


def test_concurrent_writers_do_not_share_a_temporary_file(tmp_path):
    """Every writer publishes its own file; a shared `<target>.tmp` fails on Windows.

    A fixed temporary name is not merely a lost update: two processes holding the
    same name make `os.replace` itself raise `WinError 32`, so the reader can lose
    the file entirely. A private name per writer removes both outcomes.
    """
    target = str(tmp_path / "manifest.json")
    writers = [f"writer-{index}" for index in range(4)]
    script = (
        "import sys;"
        "from wenyi_core.storage.run_store import RunStore;"
        "RunStore._write_json(sys.argv[1], {'writer': sys.argv[2]})"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, target, payload],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for payload in writers
    ]
    results = [process.communicate(timeout=120) for process in processes]
    for process, (_, errors) in zip(processes, results, strict=True):
        assert process.returncode == 0, errors

    published = json.loads(Path(target).read_text(encoding="utf-8"))
    assert published["writer"] in writers
    # A successful write leaves no temporary file beside the committed target.
    assert [path.name for path in tmp_path.iterdir()] == ["manifest.json"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("CON", "_CON"),
        ("PRN", "_PRN"),
        ("AUX", "_AUX"),
        ("NUL", "_NUL"),
        ("COM1", "_COM1"),
        ("LPT9", "_LPT9"),
        ("con", "_con"),
        ("book.", "book"),
        ("book ", "book"),
        ("book...  ", "book"),
        ("  ", "book"),
        ("...", "book"),
        ("CONSOLE", "CONSOLE"),
        ("COM10", "COM10"),
        ("サンプル小説", "サンプル小説"),
    ],
)
def test_slugify_avoids_reserved_names_and_trailing_dots(title, expected):
    """Reserved names are a filesystem rule, so the check is a pure function.

    The development drive is ReFS, which accepts `mkdir("CON")`; an assertion that
    relied on the directory call failing would pass here and fail on NTFS.
    """
    assert slugify(title) == expected


@pytest.mark.parametrize(
    "key",
    [
        "source/.wenyi-cover.tmp",
        "source/.timing-notes.tmp",
        "source/other." + "a" * 32 + ".tmp",
        "source/.wenyi-" + "b" * 32 + ".tmp",
    ],
)
def test_initialization_preserves_published_artifact_with_temporary_looking_name(tmp_path, key):
    storage = RunStore(str(tmp_path))
    storage.write_artifact(key, {"cover": "published"})

    with storage.lock():
        storage.begin_initialization("a" * 64)

    assert storage.read_artifact(key) == {"cover": "published"}


def test_initialization_keeps_unattributed_old_temporary_files(tmp_path):
    storage = RunStore(str(tmp_path))
    names = [
        "manifest.json.tmp",
        "analysis.json." + "a" * 32 + ".tmp",
        ".wenyi-manifest.json." + "b" * 32 + ".tmp",
        ".timing-abcdef12.tmp",
        "user-draft.tmp",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"unknown owner")

    with storage.lock():
        storage.begin_initialization("a" * 64)

    assert all((tmp_path / name).read_bytes() == b"unknown owner" for name in names)


def test_initialization_rebuilds_chapters_after_interrupted_state_writer(tmp_path):
    storage = RunStore(str(tmp_path))
    script = """
import os
import sys
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.storage.run_store import RunStore

def exit_before_publish(source, target):
    os._exit(42)

os.replace = exit_before_publish
store = RunStore(sys.argv[1])
with store.lock():
    store.save_chapter(Chapter(index=0, segments=[Segment(index=0, source="partial")]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, storage.run_dir], capture_output=True, timeout=30
    )
    assert result.returncode == 42, result.stderr
    (temporary,) = (tmp_path / "chapters").iterdir()
    assert json.loads(temporary.read_bytes())["segments"][0]["source"] == "partial"

    with storage.lock():
        storage.begin_initialization("a" * 64)

    assert list((tmp_path / "chapters").iterdir()) == []
    assert storage.read_artifact(".initializing.json") == {"source_sha256": "a" * 64}


def test_initialization_does_not_interrupt_an_export_in_another_lock_domain(tmp_path):
    exporting = RunStore(str(tmp_path))
    initializing = RunStore(str(tmp_path))
    target = tmp_path / "book.txt"

    with exporting.assemble_lock(), publish_target(target) as temporary:
        temporary.write_bytes(b"complete export")
        with initializing.lock():
            initializing.begin_initialization("a" * 64)
        assert temporary.read_bytes() == b"complete export"

    assert target.read_bytes() == b"complete export"


@pytest.mark.parametrize("linked_root", [False, True])
def test_initialization_has_no_temporary_sweep_through_symlink(tmp_path, linked_root):
    root, destination = tmp_path / "run", tmp_path / "destination"
    destination.mkdir()
    if not linked_root:
        root.mkdir()
    link = root if linked_root else root / "source"
    try:
        link.symlink_to(destination, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symlinks are unavailable on this platform")
    staged_looking = destination / (".wenyi-" + "a" * 32 + ".tmp")
    staged_looking.write_bytes(b"not owned by initialization")
    storage = RunStore(str(root))

    with storage.lock():
        storage.begin_initialization("a" * 64)

    assert staged_looking.read_bytes() == b"not owned by initialization"


@pytest.mark.skipif(os.name != "nt", reason="Directory junctions are Windows-specific")
def test_initialization_has_no_temporary_sweep_through_windows_junction(tmp_path):
    root, destination = tmp_path / "run", tmp_path / "destination"
    storage = RunStore(str(root))
    destination.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "source"), str(destination)],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    staged_looking = destination / (".wenyi-" + "a" * 32 + ".tmp")
    staged_looking.write_bytes(b"not owned by initialization")

    with storage.lock():
        storage.begin_initialization("a" * 64)

    assert staged_looking.read_bytes() == b"not owned by initialization"


def test_replacement_retries_while_another_process_holds_the_target(tmp_path):
    """A target held open elsewhere makes `os.replace` fail with `WinError 5`.

    Windows refuses the replacement for as long as any process has the target
    open, so a single attempt turns a routine antivirus scan into a failed write.
    Retrying while the holder lets go is what makes the write succeed.
    """
    from threading import Timer

    target = tmp_path / "manifest.json"
    target.write_text('{"target": "original"}', encoding="utf-8")
    holder = (target).open("r+b")
    released = Timer(0.5, holder.close)
    released.start()
    try:
        RunStore._write_json(str(target), {"target": "updated"})
    finally:
        released.cancel()
        holder.close()

    assert json.loads(target.read_text(encoding="utf-8")) == {"target": "updated"}
    assert [path.name for path in tmp_path.iterdir()] == ["manifest.json"]


@pytest.mark.skipif(
    os.name != "nt",
    reason="Only Windows refuses to replace a file another handle holds open",
)
def test_replacement_reports_a_target_that_stays_locked(tmp_path, monkeypatch):
    """The retry is bounded: a permanent holder is reported instead of hung on."""
    import wenyi_core.storage.atomic_write as atomic_write

    monkeypatch.setattr(atomic_write, "REPLACE_DELAY", 0.01)
    target = tmp_path / "manifest.json"
    target.write_text('{"target": "original"}', encoding="utf-8")
    attempts = []
    original_replace = os.replace

    def counting_replace(source, destination):
        attempts.append(source)
        original_replace(source, destination)

    monkeypatch.setattr(atomic_write.os, "replace", counting_replace)
    with target.open("r+b") as holder:
        with pytest.raises(PermissionError, match="Cannot replace"):
            RunStore._write_json(str(target), {"target": "updated"})
        assert holder is not None

    assert len(attempts) == atomic_write.REPLACE_ATTEMPTS
    # The previous content survives and the abandoned temporary file is removed.
    assert json.loads(target.read_text(encoding="utf-8")) == {"target": "original"}
    assert [path.name for path in tmp_path.iterdir()] == ["manifest.json"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX lets replace swap a file out from under an open handle",
)
def test_replacement_succeeds_while_the_target_is_open(tmp_path):
    """The same call on the platform that permits it still lands and cleans up.

    Windows needs the bounded retry above; POSIX never reaches it, so this branch
    pins what does happen there instead of leaving the case untested.
    """
    target = tmp_path / "manifest.json"
    target.write_text('{"target": "original"}', encoding="utf-8")
    with target.open("r+b") as holder:
        RunStore._write_json(str(target), {"target": "updated"})
        assert holder is not None

    assert json.loads(target.read_text(encoding="utf-8")) == {"target": "updated"}
    assert [path.name for path in tmp_path.iterdir()] == ["manifest.json"]


def _pending_store(tmp_path) -> FileStorage:
    """A run with one pending chapter, ready for its translation result."""
    storage = FileStorage(str(tmp_path / "run"))
    storage.save_chapter(Chapter(index=0, segments=[Segment(index=0, source="Alice waited.")]))
    storage.save_manifest(
        {
            "title": "book",
            "source_lang": "en",
            "target_lang": "zh",
            "source_sha256": "0" * 64,
            "chapters": [{"index": 0, "status": STATUS_PENDING}],
        }
    )
    return storage


def _translated_chapter() -> Chapter:
    return Chapter(
        index=0, segments=[Segment(index=0, source="Alice waited.", target="爱丽丝等着。")]
    )


def test_save_chapter_with_status_publishes_content_and_status_together(tmp_path):
    """One call must leave the chapter file and the manifest agreeing."""
    storage = _pending_store(tmp_path)
    storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)
    assert storage.load_chapter(0).segments[0].target == "爱丽丝等着。"
    assert storage.load_manifest()["chapters"][0]["status"] == STATUS_DONE
    assert storage.pending_chapters() == []


def test_second_formal_replace_failure_is_invisible_to_public_readers(tmp_path, monkeypatch):
    storage = _pending_store(tmp_path)
    manifest_path = Path(storage.manifest_path)
    chapter_path = Path(storage.chapter_path(0))
    old_manifest = manifest_path.read_bytes()
    stale_marker = Path(storage.initialization_path)
    stale_marker.write_text('{"source_sha256": "' + "0" * 64 + '"}', encoding="utf-8")
    real_replace = os.replace

    def fail_manifest(source, target):
        if os.path.basename(target) == "manifest.json":
            raise OSError("cannot materialize manifest")
        real_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_manifest)
        with pytest.raises(OSError, match="cannot materialize manifest"):
            storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)

        assert manifest_path.read_bytes() == old_manifest
        assert json.loads(chapter_path.read_bytes())["segments"][0]["target"] == "爱丽丝等着。"
        # A read-only reopen (as used by CLI status) must not replay or clean up on disk.
        reopened = RunStore(storage.run_dir, create=False)
        assert reopened.load_manifest()["chapters"][0]["status"] == STATUS_DONE
        assert reopened.pending_chapters() == []
        assert reopened.load_chapter(0) == _translated_chapter()
        artifact_reader = FileStorage(storage.run_dir, create=False)
        assert artifact_reader.read_artifact("manifest.json") == reopened.load_manifest()
        assert (
            artifact_reader.read_artifact("chapters/ch0.json") == _translated_chapter().model_dump()
        )
        snapshot = artifact_reader.create_export_snapshot(actual_sha256="0" * 64)
        assert snapshot.load_manifest()["chapters"][0]["status"] == STATUS_DONE
        assert snapshot.load_chapter(0) == _translated_chapter()
        assert manifest_path.read_bytes() == old_manifest
        assert stale_marker.is_file()
        with pytest.raises(ValueError, match="Input content does not match"):
            reopened.create_export_snapshot(actual_sha256="1" * 64)

    # A subsequent writer materializes the same generation before modifying it.
    with artifact_reader.lock():
        chapter = artifact_reader.load_chapter(0)
        chapter.segments[0].target = "edited"
        artifact_reader.save_chapter(chapter)
        assert json.loads(manifest_path.read_bytes())["chapters"][0]["status"] == STATUS_DONE
    assert artifact_reader.load_chapter(0).segments[0].target == "edited"
    assert snapshot.load_chapter(0) == _translated_chapter()
    assert snapshot.read_artifact("chapters/ch0.json") == _translated_chapter().model_dump()


def test_export_snapshot_waits_for_in_flight_chapter_publication(tmp_path, monkeypatch):
    storage = _pending_store(tmp_path)
    reader = FileStorage(storage.run_dir, create=False)
    chapter_replaced = Event()
    snapshot_started = Event()
    snapshot_finished = Event()
    release_writer = Event()
    real_replace = os.replace
    errors = []
    snapshots = []

    def replace_then_pause(temp, target):
        real_replace(temp, target)
        if os.path.basename(target) == "ch0.json":
            chapter_replaced.set()
            assert release_writer.wait(10)

    def publish():
        try:
            storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)
        except BaseException as error:
            errors.append(error)

    def export():
        snapshot_started.set()
        try:
            snapshots.append(reader.create_export_snapshot(actual_sha256="0" * 64))
        except BaseException as error:
            errors.append(error)
        finally:
            snapshot_finished.set()

    monkeypatch.setattr(os, "replace", replace_then_pause)
    writer = Thread(target=publish)
    exporter = Thread(target=export)
    writer.start()
    try:
        assert chapter_replaced.wait(10)
        exporter.start()
        assert snapshot_started.wait(10)
        assert not snapshot_finished.wait(0.1)
    finally:
        release_writer.set()
        writer.join(10)
        if exporter.ident is not None:
            exporter.join(10)

    assert not errors
    (snapshot,) = snapshots
    assert snapshot.load_chapter(0) == _translated_chapter()
    assert snapshot.load_manifest()["chapters"][0]["status"] == STATUS_DONE


def test_uncommitted_chapter_journal_is_not_listed_after_process_exit(tmp_path):
    storage = _pending_store(tmp_path)
    public_key = ".wenyi-" + "a" * 32 + ".tmp"
    storage.write_artifact(public_key, {"published": True})
    listed = storage.list_artifacts()
    run = Path(storage.run_dir)
    entries = set(run.iterdir())
    script = """
import os
import sys
from wenyi_core.storage.chapter_publication import JOURNAL_NAME
from wenyi_core.storage.file import FileStorage

replace = os.replace
def exit_before_journal_replace(source, target):
    if os.path.basename(target) == JOURNAL_NAME:
        os._exit(42)
    replace(source, target)

os.replace = exit_before_journal_replace
store = FileStorage(sys.argv[1], create=False)
chapter = store.load_chapter(0)
chapter.segments[0].target = "not yet committed"
store.save_chapter_with_status(chapter, "done")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, storage.run_dir], capture_output=True, timeout=30
    )
    assert result.returncode == 42, result.stderr
    (staging,) = set(run.iterdir()) - entries
    assert "not yet committed" in json.loads(staging.read_bytes())["chapter"]["text"]

    reopened = FileStorage(storage.run_dir, create=False)
    assert public_key in listed
    assert reopened.list_artifacts() == listed
    assert reopened.read_artifact(JOURNAL_NAME) is None
    assert reopened.pending_chapters() == [0]
    assert reopened.load_chapter(0).segments[0].target is None


def test_chapter_publication_survives_process_exit_between_formal_replacements(tmp_path):
    storage = _pending_store(tmp_path)
    script = """
import os
import sys
from wenyi_core.storage.file import FileStorage

store = FileStorage(sys.argv[1], create=False)
chapter = store.load_chapter(0)
chapter.segments[0].target = ""
replace = os.replace

def exit_after_chapter(source, target):
    replace(source, target)
    if os.path.basename(target) == "ch0.json":
        os._exit(42)

os.replace = exit_after_chapter
store.save_chapter_with_status(chapter, "done")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, storage.run_dir],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 42, result.stderr

    reopened = FileStorage(storage.run_dir, create=False)
    assert reopened.pending_chapters() == []
    assert reopened.load_chapter(0).segments[0].target == ""
    # Recovery on a modifying resume runs only once.
    with reopened.lock():
        reopened.ensure_source_identity("", actual_sha256="0" * 64)
        assert reopened.pending_chapters() == []
    with reopened.lock():
        reopened.ensure_source_identity("", actual_sha256="0" * 64)
        assert reopened.pending_chapters() == []
    assert (
        json.loads(Path(storage.manifest_path).read_bytes())["chapters"][0]["status"] == STATUS_DONE
    )


@pytest.mark.parametrize("foreign_state", [{"source_sha256": "1" * 64}, {"title": "edited"}])
def test_pending_chapter_publication_refuses_changed_source_state(
    tmp_path, monkeypatch, foreign_state
):
    storage = _pending_store(tmp_path)
    old = storage.load_manifest()
    real_replace = os.replace

    def fail_manifest(source, target):
        if os.path.basename(target) == "manifest.json":
            raise OSError("cannot materialize manifest")
        real_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_manifest)
        with pytest.raises(OSError, match="cannot materialize manifest"):
            storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)

    foreign_manifest = {**old, **foreign_state}
    RunStore._write_json(storage.manifest_path, foreign_manifest)
    reopened = FileStorage(storage.run_dir, create=False)
    with pytest.raises(ValueError, match="pending chapter publication"):
        reopened.load_manifest()
    with pytest.raises(ValueError, match="pending chapter publication"):
        reopened.load_chapter(0)
    with reopened.lock(), pytest.raises(ValueError, match="pending chapter publication"):
        reopened.ensure_source_identity("", actual_sha256="1" * 64)
    assert RunStore._read_json(storage.manifest_path) == foreign_manifest


def test_save_chapter_with_status_commits_the_intent_first(tmp_path, monkeypatch):
    """A durable after-image must be available before replacing either canonical file."""
    storage = _pending_store(tmp_path)
    published = []
    real_replace = os.replace

    def recording_replace(source, target):
        published.append(os.path.basename(str(target)))
        real_replace(source, target)

    monkeypatch.setattr("wenyi_core.storage.run_store.os.replace", recording_replace)
    storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)
    assert published == [JOURNAL_NAME, "ch0.json", "manifest.json"]
    assert not (Path(storage.run_dir) / JOURNAL_NAME).exists()


def test_save_chapter_with_status_keeps_previous_state_when_intent_write_fails(
    tmp_path, monkeypatch
):
    """A disk failure before the journal is published leaves the old generation."""
    storage = _pending_store(tmp_path)
    old_chapter = storage.load_chapter(0)
    old_manifest = storage.load_manifest()

    def fail_fsync(descriptor):
        raise OSError("no space left for intent")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="no space left for intent"):
        storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)
    assert storage.load_chapter(0) == old_chapter
    assert storage.load_manifest() == old_manifest
    assert not (Path(storage.run_dir) / JOURNAL_NAME).exists()
    assert [path.name for path in (tmp_path / "run").rglob("*.tmp")] == []


@pytest.mark.parametrize("payload", ["{incomplete", '{"version": 0, "index": 0}'])
def test_unreadable_chapter_intent_blocks_reads_and_new_writes(tmp_path, payload):
    storage = _pending_store(tmp_path)
    marker = Path(storage.run_dir) / JOURNAL_NAME
    marker.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="pending chapter publication"):
        storage.load_manifest()
    with pytest.raises(ValueError, match="pending chapter publication"):
        storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)
    assert marker.read_text(encoding="utf-8") == payload


def test_retry_after_journal_removal_failure_does_not_rewrite_formal_files(tmp_path, monkeypatch):
    storage = _pending_store(tmp_path)
    marker = Path(storage.run_dir) / JOURNAL_NAME
    real_unlink = os.unlink

    def fail_retirement(path, *args, **kwargs):
        if os.path.basename(path) == JOURNAL_NAME:
            raise OSError("cannot retire journal")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_retirement)
        with pytest.raises(OSError, match="cannot retire journal"):
            storage.save_chapter_with_status(_translated_chapter(), STATUS_DONE)

    assert marker.is_file()
    assert storage.pending_chapters() == []

    def no_replace(*_):
        pytest.fail("recovery replaced a file that was already materialized")

    monkeypatch.setattr(os, "replace", no_replace)
    with storage.lock():
        storage.ensure_source_identity("", actual_sha256="0" * 64)
    assert not marker.exists()


#: Filesystem calls that need the extended-length spelling, and the ones that do not.
#: \`os.path.join\` only builds a string, so prefixing its result would be wrong:
#: the caller decides which namespace the finished path is opened in.
_FILESYSTEM_CALLS = (
    "open(",
    "os.remove(",
    "os.unlink(",
    "os.replace(",
    "os.makedirs(",
    "os.listdir(",
    "os.scandir(",
    "os.stat(",
    "os.path.isfile(",
    "os.path.isdir(",
    "os.path.exists(",
    "shutil.rmtree(",
)


def _naked_calls(path: Path, calls: tuple[str, ...]) -> list[str]:
    """Lines in this module that call some filesystem function on a bare path."""
    naked = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or not any(call in line for call in calls):
            continue
        if "to_io_path" in line:
            continue
        naked.append(f"{path.name}:{number}: {stripped}")
    return naked


def test_runstore_reaches_the_filesystem_only_through_the_extended_length_form():
    """Every filesystem call in the run store must open a path it can actually reach.

    A single long-path test can only prove the one path it walks, so this is the
    complementary check: the generic helper that writes every run file goes
    through the prefix, and the calls spelled here do too. The join and dirname
    helpers are excluded because they build a string without opening anything;
    prefixing there would hand a prefixed path to the caller's own open call.
    """
    module = Path(__file__).resolve().parents[1] / "wenyi_core" / "storage" / "run_store.py"
    assert _naked_calls(module, _FILESYSTEM_CALLS) == []


def test_atomic_write_reaches_the_filesystem_through_the_extended_length_form():
    """The writer every run file shares must not open a bare path."""
    module = Path(__file__).resolve().parents[1] / "wenyi_core" / "storage" / "atomic_write.py"
    assert _naked_calls(module, _FILESYSTEM_CALLS) == []


def test_storage_modules_do_not_open_files_without_the_extended_length_form():
    """The artifact port and the SQLite guard are the other two writers of state."""
    storage = Path(__file__).resolve().parents[1] / "wenyi_core" / "storage"
    naked = _naked_calls(storage / "artifacts.py", _FILESYSTEM_CALLS)
    assert naked == []
