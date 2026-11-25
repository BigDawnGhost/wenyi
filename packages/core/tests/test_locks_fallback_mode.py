"""Pin the WENYI_LOCKS_MODE fallback policy of the native lock loader.

The wenyi-locks native library is optional: when it cannot be loaded the
module must say so loudly (default), fail on demand, or stay quiet only when
the operator says so. Every case fakes the ctypes load, so the policy is
exercised without a built crate.
"""

from __future__ import annotations

import logging

import pytest
from wenyi_core.storage import locks


def _reset_probe(monkeypatch, candidates=("absent-one", "absent-two")) -> None:
    """Serve a fresh probe that always fails, as if no crate were built."""
    monkeypatch.setattr(locks, "_native_probed", False)
    monkeypatch.setattr(locks, "_native", None)
    monkeypatch.setattr(locks, "_native_attempts", [])
    monkeypatch.delenv("WENYI_LOCKS_LIB", raising=False)
    monkeypatch.setattr(locks, "_library_candidates", lambda: iter(candidates))

    def load(_: str):
        raise OSError("cannot load")

    monkeypatch.setattr(locks.ctypes, "CDLL", load)


def test_default_mode_warns_once_with_the_real_reasons(monkeypatch, caplog):
    _reset_probe(monkeypatch)
    monkeypatch.delenv("WENYI_LOCKS_MODE", raising=False)
    with caplog.at_level(logging.WARNING, logger=locks.__name__):
        assert locks._native_library() is None
        assert locks._native_library() is None
    records = [record for record in caplog.records if record.name == locks.__name__]
    assert [record.levelname for record in records] == ["WARNING"]
    message = records[0].getMessage()
    assert "absent-two (cannot load)" in message
    assert "Python backend" in message
    assert "WENYI_LOCKS_MODE=error" in message


def test_error_mode_fails_with_the_load_reason_instead_of_falling_back(monkeypatch):
    _reset_probe(monkeypatch)
    monkeypatch.setenv("WENYI_LOCKS_MODE", "error")
    with pytest.raises(RuntimeError) as first:
        locks._native_library()
    assert "absent-one (cannot load)" in str(first.value)
    assert "WENYI_LOCKS_MODE=warn" in str(first.value)
    # The probe is cached; the policy stays deterministic on later calls.
    with pytest.raises(RuntimeError):
        locks._native_library()


def test_error_mode_rejects_a_native_acquisition(tmp_path, monkeypatch):
    _reset_probe(monkeypatch)
    monkeypatch.setenv("WENYI_LOCKS_MODE", "error")
    monkeypatch.setattr(locks, "_FORCED_BACKEND", "rust")
    handle = locks.FileLockHandle(str(tmp_path / ".run.lock"))
    with pytest.raises(RuntimeError, match="wenyi-locks native library"):
        handle.acquire()
    assert handle.backend is None


def test_silent_mode_keeps_the_fallback_without_a_warning(monkeypatch, caplog):
    _reset_probe(monkeypatch)
    monkeypatch.setenv("WENYI_LOCKS_MODE", "silent")
    with caplog.at_level(logging.WARNING, logger=locks.__name__):
        assert locks._native_library() is None
    assert [record for record in caplog.records if record.name == locks.__name__] == []


def test_unknown_mode_value_is_rejected(monkeypatch):
    _reset_probe(monkeypatch)
    monkeypatch.setenv("WENYI_LOCKS_MODE", "loud")
    with pytest.raises(ValueError, match="lock mode"):
        locks._native_library()


def test_error_mode_leaves_an_explicit_python_backend_working(tmp_path, monkeypatch):
    _reset_probe(monkeypatch)
    monkeypatch.setenv("WENYI_LOCKS_MODE", "error")
    monkeypatch.setattr(locks, "_FORCED_BACKEND", "python")
    with locks.exclusive_file_lock(str(tmp_path / ".run.lock"), blocking=False):
        pass
