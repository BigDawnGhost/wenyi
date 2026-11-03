"""File artifacts keep portable keys and restrict scans to the requested directory."""

import ntpath
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest
from wenyi_core.errors import ArtifactCorrupt, WenyiError
from wenyi_core.storage.artifacts import FileArtifacts

LONG_PREFIX = "precision/" + "a" * 100 + "/" + "b" * 100 + "/" + "c" * 50 + "/"


@pytest.fixture
def windows_paths(monkeypatch):
    """Model Windows resolution without changing the host's pathlib or os module."""
    import wenyi_core.storage.artifacts as artifacts

    class ResolvingWindowsPath(PureWindowsPath):
        def resolve(self):
            text = ntpath.normpath(str(self))
            if len(text) > 260 and not text.startswith("\\\\?\\"):
                text = "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
            return type(self)(text)

    # to_io_path lives in its own module and reads *its* os, so patching only the
    # artifacts module would leave the real platform in place and make these
    # Windows-shaped expectations fail wherever the host is not Windows.
    import wenyi_core.storage.io_path as io_path

    windows_os = SimpleNamespace(name="nt", path=ntpath, sep="\\")
    monkeypatch.setattr(artifacts, "os", windows_os)
    monkeypatch.setattr(artifacts, "Path", ResolvingWindowsPath)
    monkeypatch.setattr(io_path, "os", windows_os)
    return ResolvingWindowsPath


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\state\run", r"\\?\C:\state\run"),
        (r"\\server\share\run", r"\\?\UNC\server\share\run"),
        (r"\\?\C:\state\run", r"\\?\C:\state\run"),
        (r"\\?\UNC\server\share\run", r"\\?\UNC\server\share\run"),
        ("//server/share/run", r"\\?\UNC\server\share\run"),
        ("//?/C:/state/run", r"\\?\C:\state\run"),
    ],
)
def test_windows_io_paths_use_valid_extended_syntax(windows_paths, path, expected):
    assert FileArtifacts._io_path(path) == expected
    assert FileArtifacts._io_path(windows_paths(path)) == expected


@pytest.mark.parametrize("run_dir", [r"C:\state\run", r"\\server\share\run"])
@pytest.mark.parametrize("key", ["report.json", LONG_PREFIX + "result.json"])
def test_windows_containment_accepts_short_and_long_keys(windows_paths, run_dir, key):
    storage = FileArtifacts(run_dir)
    path = storage._artifact_path(key)
    root = windows_paths(FileArtifacts._io_path(run_dir)).resolve()
    assert path.is_relative_to(root)
    assert path.relative_to(root).as_posix() == key


@pytest.mark.parametrize("run_dir", [r"C:\state\run", r"\\server\share\run"])
@pytest.mark.parametrize(
    "key",
    [
        "../outside.json",
        r"..\outside.json",
        r"C:\other\outside.json",
        r"\\server\other\outside.json",
        r"\\?\C:\other\outside.json",
    ],
)
def test_windows_containment_rejects_escape_keys(windows_paths, run_dir, key):
    with pytest.raises(ValueError, match="Artifact key must remain within the run"):
        FileArtifacts(run_dir)._artifact_path(key)


@pytest.mark.parametrize("run_dir", [r"C:\state\run", r"\\server\share\run"])
def test_windows_listing_uses_one_path_namespace(windows_paths, monkeypatch, run_dir):
    storage = FileArtifacts(run_dir)
    scans = []

    def scan(directory, pattern):
        assert pattern == "*"
        scans.append(directory)
        yield directory / "result.json"

    monkeypatch.setattr(windows_paths, "is_dir", lambda _: True, raising=False)
    monkeypatch.setattr(windows_paths, "is_file", lambda _: True, raising=False)
    monkeypatch.setattr(windows_paths, "rglob", scan, raising=False)
    assert storage.list_artifacts(LONG_PREFIX) == [LONG_PREFIX + "result.json"]
    assert scans == [windows_paths(FileArtifacts._io_path(run_dir), LONG_PREFIX).resolve()]


@pytest.mark.skipif(os.name == "nt", reason="Backslashes are filename characters on POSIX")
@pytest.mark.parametrize(
    ("root_name", "key"),
    [("run", "../run\\"), ("run\\", "../run/escape.json")],
)
def test_posix_backslashes_do_not_allow_artifact_escape(tmp_path, root_name, key):
    storage = FileArtifacts(str(tmp_path / root_name))
    with pytest.raises(ValueError, match="Artifact key must remain within the run"):
        storage.write_artifact(key, {"outside": True})


@pytest.mark.skipif(os.name == "nt", reason="Backslashes are filename characters on POSIX")
def test_posix_backslashes_are_preserved_in_artifact_names(tmp_path):
    storage = FileArtifacts(str(tmp_path / "run\\"))
    key = "draft\\"
    storage.write_artifact(key, {"target": "translation"})
    assert storage.read_artifact(key) == {"target": "translation"}
    assert storage.list_artifacts() == [key]


@pytest.mark.parametrize("key", ["../outside.json", "../../outside.json"])
def test_artifact_containment_rejects_traversal(tmp_path, key):
    with pytest.raises(ValueError, match="Artifact key must remain within the run"):
        FileArtifacts(str(tmp_path / "run")).write_artifact(key, {})


def test_artifact_containment_rejects_absolute_escape(tmp_path):
    storage = FileArtifacts(str(tmp_path / "run"))
    with pytest.raises(ValueError, match="Artifact key must remain within the run"):
        storage.write_artifact(str(tmp_path / "outside.json"), {})


def test_artifact_containment_rejects_symlink_escape(tmp_path):
    root, outside = tmp_path / "run", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symlinks are unavailable on this platform")
    storage = FileArtifacts(str(root))
    with pytest.raises(ValueError, match="Artifact key must remain within the run"):
        storage.write_artifact("link/escape.json", {})
    assert not (outside / "escape.json").exists()


def test_long_artifacts_support_read_write_list_records_and_delete(tmp_path):
    storage = FileArtifacts(str(tmp_path / "run"))
    key, records_key = LONG_PREFIX + "result.json", LONG_PREFIX + "events.jsonl"
    assert len(str(tmp_path / "run" / key)) > 260
    storage.write_artifact(key, {"target": "first"})
    storage.write_artifact(key, {"target": "updated"})
    assert storage.read_artifact(key) == {"target": "updated"}
    storage.append_artifact_record(records_key, {"event": "first"})
    storage.append_artifact_record(records_key, {"event": "second"})
    assert storage.read_artifact_records(records_key) == [
        {"event": "first"},
        {"event": "second"},
    ]
    assert storage.list_artifacts(LONG_PREFIX) == sorted([key, records_key])
    assert storage.list_artifacts() == sorted([key, records_key])
    storage.delete_artifact(key)
    storage.delete_artifact(key)
    assert storage.read_artifact(key) is None
    assert storage.list_artifacts(LONG_PREFIX) == [records_key]
    storage.delete_artifact(records_key)
    assert storage.read_artifact_records(records_key) == []
    assert storage.list_artifacts(LONG_PREFIX) == []


def test_long_run_directory_can_resume_review(tmp_path):
    from wenyi_core.review.run_store import ReviewRunStore

    run_dir = tmp_path / ("a" * 100) / ("b" * 100) / ("c" * 80)
    assert len(str(run_dir)) > 260
    storage = FileArtifacts(str(run_dir))
    review = ReviewRunStore(str(run_dir), storage=storage)
    review.start(reviewed_content_digest="digest", metadata={})
    review.mark_chunk_done("r1-ch0-base0-n1", {"issues": []})
    review.mark_interrupted()

    restored = ReviewRunStore.find_resumable(str(run_dir), "digest", storage=storage)
    assert restored is not None
    assert restored.review_id == review.review_id
    assert restored.load_chunk_result("r1-ch0-base0-n1") == {"issues": []}
    assert all("\\" not in key for key in storage.list_artifacts("reviews/"))


def test_windows_paths_can_resume_review(tmp_path, monkeypatch):
    from wenyi_core.review.run_store import ReviewRunStore

    storage = FileArtifacts(str(tmp_path))
    review = ReviewRunStore(str(tmp_path), storage=storage)
    review.start(reviewed_content_digest="digest", metadata={})
    review.mark_chunk_done("r1-ch0-base0-n1", {"issues": []})
    review.mark_interrupted()

    original = Path.relative_to

    def windows_relative_to(path, *args, **kwargs):
        return PureWindowsPath(original(path, *args, **kwargs).as_posix())

    # Exercise native Windows relative-path formatting on every test platform.
    monkeypatch.setattr(Path, "relative_to", windows_relative_to)
    restored = ReviewRunStore.find_resumable(str(tmp_path), "digest", storage=storage)
    assert restored is not None
    assert restored.review_id == review.review_id
    assert restored.load_chunk_result("r1-ch0-base0-n1") == {"issues": []}


@pytest.mark.parametrize("prefix", ["reviews/", "reviews/review-a/chunks/c"])
def test_prefix_scan_does_not_traverse_source_files(tmp_path, monkeypatch, prefix):
    storage = FileArtifacts(str(tmp_path))
    storage.write_artifact("reviews/review-a/chunks/ch0.json", {})
    storage.write_artifact("source/parser-cache.json", {})
    scans = []
    native_path = type(tmp_path)
    original = native_path.rglob

    def record_scan(path, pattern):
        scans.append(path)
        return original(path, pattern)

    monkeypatch.setattr(native_path, "rglob", record_scan)
    assert storage.list_artifacts(prefix) == ["reviews/review-a/chunks/ch0.json"]
    expected_directory = Path(storage._io_path(tmp_path / prefix.rpartition("/")[0])).resolve()
    assert scans == [expected_directory]


def test_artifact_listing_keeps_prefix_semantics(tmp_path):
    storage = FileArtifacts(str(tmp_path))
    keys = ["reviews/review-b/manifest.json", "reviews/review-a/manifest.json", "usage.json"]
    for key in keys:
        storage.write_artifact(key, {})
    assert storage.list_artifacts() == sorted(keys)
    assert storage.list_artifacts("reviews/review-") == sorted(keys[:2])
    assert storage.list_artifacts("reviews/review-a/manifest.json") == [keys[1]]
    assert storage.list_artifacts("reviews/missing/") == []


def test_corrupt_artifact_raises_instead_of_reading_as_missing(tmp_path):
    """Corruption must surface, never masquerade as a cache miss.

    A cache miss means recompute, and recomputing a paid model product bills
    the user again. Corrupt data has to be visible so a person can decide.
    """
    artifacts = FileArtifacts(str(tmp_path))
    artifacts.write_artifact("precision/ch0.json", {"targets": ["a"]})

    assert artifacts.read_artifact("precision/missing.json") is None

    path = Path(tmp_path) / "precision" / "ch0.json"
    path.write_text('{"targets": ["a"', encoding="utf-8")

    with pytest.raises(ArtifactCorrupt) as error:
        artifacts.read_artifact("precision/ch0.json")
    assert isinstance(error.value, WenyiError)
    assert error.value.code == "artifact_corrupt"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"\\?\C:\Users\Reader\Wenyi Desktop", r"C:\Users\Reader\Wenyi Desktop"),
        (r"\\?\D:\工作区 # 100%\Wenyi", r"D:\工作区 # 100%\Wenyi"),
        (r"\\?\UNC\server\share\Wenyi Desktop", r"\\server\share\Wenyi Desktop"),
        (r"C:\Users\Reader\Wenyi Desktop", r"C:\Users\Reader\Wenyi Desktop"),
        (r"\\server\share\Wenyi Desktop", r"\\server\share\Wenyi Desktop"),
        # A device namespace and a volume GUID have no ordinary spelling, so
        # unwrapping them would name a different object rather than the same one.
        (r"\\.\C:\Wenyi", r"\\.\C:\Wenyi"),
        (r"\\?\Volume{example}\Wenyi", r"\\?\Volume{example}\Wenyi"),
        ("/tmp/Wenyi Desktop", "/tmp/Wenyi Desktop"),
    ],
)
def test_plain_path_reports_the_spelling_a_person_recognises(path, expected):
    """A path shown to a person must not carry the opening-time prefix."""
    from wenyi_core.storage.io_path import plain_path

    assert plain_path(path) == expected


def test_plain_path_undoes_the_prefix_this_platform_adds():
    """The two spellings are inverses, so reporting never drifts from opening."""
    from wenyi_core.storage.io_path import plain_path, to_io_path

    assert plain_path(to_io_path(r"C:\state\run")) == r"C:\state\run"


def test_sqlite_paths_are_length_checked_instead_of_prefixed():
    """SQLite rejects the extended-length prefix, so a long path has to fail loudly.

    A URI built from a prefixed path percent-encodes the separator, and SQLite
    reads that as an authority: the connection fails with "unable to open
    database file" and says nothing about the length.
    """
    from wenyi_core.storage.io_path import SQLITE_PATH_LIMIT, require_sqlite_path

    assert require_sqlite_path(r"C:\state\run\state.sqlite3") == os.path.abspath(
        r"C:\state\run\state.sqlite3"
    )
    long_path = str(Path.cwd() / "run" / ("a" * SQLITE_PATH_LIMIT) / "state.sqlite3")
    with pytest.raises(ValueError, match="SQLite cannot open a path longer than"):
        require_sqlite_path(long_path)


def test_long_run_directory_round_trips_through_the_extended_length_form(tmp_path):
    """A 305-character run directory is usable end to end, not just constructible.

    The prefix is the only spelling that makes the deep path openable, so this
    exercises a write and a read instead of asserting on the string itself.
    """
    from wenyi_core.storage.run_store import RunStore

    run_dir = tmp_path / ("a" * 100) / ("b" * 100) / ("c" * 80)
    path = run_dir / "manifest.json"
    assert len(str(path)) > 260

    RunStore(str(run_dir))._write_json(str(path), {"target": "translation"})

    assert RunStore._read_json(str(path)) == {"target": "translation"}
    assert FileArtifacts(str(run_dir)).read_artifact("manifest.json") == {"target": "translation"}
