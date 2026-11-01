"""File migration owns discovery, exact backups, locking and retryable atomic publication."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from wenyi_core.llm.usage import UsageSample, UsageTracker, convert_usage_ledger
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.file_migrations import migrate_usage_ledgers


def ledgers(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    (root / "manifest.json").write_text("{}")
    tracker = UsageTracker()
    tracker.record(
        "cheap", UsageSample(prompt_tokens=7, completion_tokens=3, total_tokens=10), "Reviewer"
    )
    current = tracker.summary()
    original = json.dumps(
        {key: current[key] for key in ("totals", "by_tier", "by_stage")}, indent=4
    ).encode()
    paths = [root / "usage.json", root / "reviews/review-test/usage.json"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
    return root, paths, original


def test_migration_backs_up_exact_bytes_and_is_idempotent(tmp_path):
    root, paths, original = ledgers(tmp_path)
    untouched = root / "reviews/not-a-review/usage.json"
    untouched.parent.mkdir()
    untouched.write_bytes(b"not a ledger")
    assert migrate_usage_ledgers(root) == 2
    for path in paths:
        assert json.loads(path.read_bytes()) == convert_usage_ledger(json.loads(original))
        assert [
            backup.read_bytes() for backup in path.parent.glob("usage.before-routing-*.json")
        ] == [original]
    assert migrate_usage_ledgers(root) == 0
    assert untouched.read_bytes() == b"not a ledger"


def test_invalid_later_ledger_prevents_all_writes(tmp_path):
    root, paths, original = ledgers(tmp_path)
    paths[1].write_text("{invalid")
    with pytest.raises(ValueError):
        migrate_usage_ledgers(root)
    assert paths[0].read_bytes() == original
    assert not list(root.rglob("usage.before-routing-*.json"))


def test_atomic_publish_failure_preserves_original_and_can_resume(tmp_path, monkeypatch):
    from wenyi_core.storage import artifacts

    root, paths, original = ledgers(tmp_path)
    replace = artifacts.os.replace

    def fail_review(source, destination):
        if destination == paths[1].resolve():
            raise OSError("publication interrupted")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(artifacts.os, "replace", fail_review)
        with pytest.raises(OSError, match="interrupted"):
            migrate_usage_ledgers(root)
    assert paths[1].read_bytes() == original
    assert next(paths[1].parent.glob("usage.before-routing-*.json")).read_bytes() == original
    assert migrate_usage_ledgers(root) == 1
    assert migrate_usage_ledgers(root) == 0


def test_migration_waits_for_run_lock(tmp_path):
    root, paths, original = ledgers(tmp_path)
    entered = Event()

    def migrate():
        entered.set()
        return migrate_usage_ledgers(root)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with FileStorage(str(root)).lock():
            result = pool.submit(migrate)
            assert entered.wait(5)
            assert not result.done()
            assert paths[0].read_bytes() == original
        assert result.result(timeout=5) == 2


def test_migration_requires_an_existing_run_without_creating_it(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="manifest"):
        migrate_usage_ledgers(missing)
    assert not missing.exists()
