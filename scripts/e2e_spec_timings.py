"""Turn a Playwright JSON report into a per-spec duration table.

The trimming decision in T-37 needs to know which specs are expensive, not
just how many there are. Playwright can emit a JSON report containing the
duration of every spec and every test inside it; this script folds that into
one Markdown table, sorted by total time, and prints it to stdout so the CI job
summary can show it next to the run.

It does not run anything and does not modify the report.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def walk_specs(suite: dict, prefix: str, rows: list[tuple[str, float, int, str, str]]) -> None:
    """Collect one row per spec, summing the durations of the results it ran."""
    for child in suite.get("suites", []):
        walk_specs(child, f"{prefix}{child.get('title', '')} › ", rows)
    for spec in suite.get("specs", []):
        # A retried test appears once with two results, so summing results
        # reports what the suite actually cost rather than the last attempt.
        seconds = (
            sum(
                result.get("duration", 0)
                for test in spec.get("tests", [])
                for result in test.get("results", [])
            )
            / 1000
        )
        statuses = {test.get("status", "unknown") for test in spec.get("tests", [])}
        overall = "passed" if statuses == {"expected"} else ",".join(sorted(statuses))
        file = spec.get("file", "")
        rows.append(
            (
                f"{prefix}{spec.get('title', '')}",
                seconds,
                len(spec.get("tests", [])),
                overall,
                file,
            )
        )


def build(path: Path, label: str) -> str:
    report = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[str, float, int, str, str]] = []
    for suite in report.get("suites", []):
        walk_specs(suite, f"{suite.get('title', '')} › ", rows)
    rows.sort(key=lambda row: row[1], reverse=True)
    total = sum(row[1] for row in rows)
    lines = [
        f"### {label} per-spec timings",
        "",
        f"{len(rows)} specs, {total:.1f} s of test time in total.",
        "",
        "| spec | seconds | tests | status |",
        "| --- | ---: | ---: | --- |",
    ]
    for title, seconds, tests, status, _ in rows:
        lines.append(f"| `{title}` | {seconds:.1f} | {tests} | {status} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Playwright JSON report.")
    parser.add_argument("--label", default="e2e", help="Suite name for the heading.")
    parser.add_argument("--output", type=Path, help="Write here instead of stdout.")
    arguments = parser.parse_args()
    if not arguments.input.is_file():
        raise SystemExit(f"No JSON report at {arguments.input}")
    text = build(arguments.input, arguments.label)
    if arguments.output:
        arguments.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
