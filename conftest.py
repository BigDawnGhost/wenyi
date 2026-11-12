"""Report retried-then-passed tests so a flaky run is never silently green.

pytest-rerunfailures logs a retried attempt with outcome "rerun". Without
surfacing it, a run that only passed on the second attempt looks clean. Track
those tests and name them at the end of the run and in GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import os

_retried: set[str] = set()


def pytest_runtest_logreport(report):
    if report.outcome == "rerun":
        _retried.add(report.nodeid)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _retried:
        return
    names = sorted(_retried)
    terminalreporter.write_sep("-", "flaky tests (passed only after a retry)")
    for nodeid in names:
        terminalreporter.write_line(f"- {nodeid}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as sink:
        sink.write("## Flaky tests (passed only after a retry)\n\n")
        for nodeid in names:
            sink.write(f"- `{nodeid}`\n")
