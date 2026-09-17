"""File artifacts keep portable keys and restrict scans to the requested directory."""

from pathlib import Path, PureWindowsPath

import pytest
from wenyi_core.storage.artifacts import FileArtifacts


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
