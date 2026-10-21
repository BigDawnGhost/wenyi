"""Deterministic deadlines for offline Desktop workflow polling."""

from types import SimpleNamespace

import desktop_test_support
import pytest


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(
        desktop_test_support,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
    )
    return clock


def test_wait_job_allows_slow_local_checkpoint_commits(clock, monkeypatch):
    def get_job(run_id):
        return {
            "run_id": run_id,
            "status": "done" if clock.now >= 12 else "running",
        }

    monkeypatch.setattr(desktop_test_support.dal, "get_job_by_arq_id", get_job)

    assert desktop_test_support.wait_job("slow-run") == {"run_id": "slow-run", "status": "done"}


@pytest.mark.parametrize("status", ["done", "error", "paused", "interrupted"])
def test_wait_job_returns_terminal_status_without_delay(clock, monkeypatch, status):
    job = {"run_id": "finished-run", "status": status}
    monkeypatch.setattr(desktop_test_support.dal, "get_job_by_arq_id", lambda _: job)

    assert desktop_test_support.wait_job("finished-run") == job
    assert clock.now == 0


@pytest.mark.parametrize("status", [None, "queued", "running"])
def test_wait_job_keeps_explicit_timeout_and_last_job_diagnostic(clock, monkeypatch, status):
    job = {"run_id": "stuck-run", "status": status} if status else None
    monkeypatch.setattr(desktop_test_support.dal, "get_job_by_arq_id", lambda _: job)

    with pytest.raises(pytest.fail.Exception) as error:
        desktop_test_support.wait_job("stuck-run", timeout=0.05)

    assert "Local task did not finish within 0.05s" in str(error.value)
    assert str(job) in str(error.value)
    assert 0.05 <= clock.now < 0.06


def test_wait_job_default_deadline_remains_bounded(clock, monkeypatch):
    job = {"run_id": "stuck-run", "status": "running"}
    monkeypatch.setattr(desktop_test_support.dal, "get_job_by_arq_id", lambda _: job)

    with pytest.raises(pytest.fail.Exception, match="Local task did not finish within 60s"):
        desktop_test_support.wait_job("stuck-run")

    assert 60 <= clock.now < 60.01
