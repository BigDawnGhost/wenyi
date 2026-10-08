"""File artifacts keep portable keys and restrict scans to the requested directory."""

import ntpath
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest
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

    monkeypatch.setattr(artifacts, "os", SimpleNamespace(name="nt", path=ntpath, sep="\\"))
    monkeypatch.setattr(artifacts, "Path", ResolvingWindowsPath)
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
    assert scans == [tmp_path / prefix.rpartition("/")[0]]


def test_artifact_listing_keeps_prefix_semantics(tmp_path):
    storage = FileArtifacts(str(tmp_path))
    keys = ["reviews/review-b/manifest.json", "reviews/review-a/manifest.json", "usage.json"]
    for key in keys:
        storage.write_artifact(key, {})
    assert storage.list_artifacts() == sorted(keys)
    assert storage.list_artifacts("reviews/review-") == sorted(keys[:2])
    assert storage.list_artifacts("reviews/review-a/manifest.json") == [keys[1]]
    assert storage.list_artifacts("reviews/missing/") == []
