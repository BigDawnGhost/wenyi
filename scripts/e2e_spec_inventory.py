"""Report how the two end-to-end suites overlap, without changing either.

The desktop suite runs many of the same specs as the web suite, because the
shared UI is exercised through both host applications. That duplication is
deliberate, but nobody has measured what it costs. This script pairs the specs
by name, says whether the desktop copy is a plain re-export or its own file,
and counts the tests behind each name, so a trimming decision can be made from
data rather than from the file listing.

It writes Markdown to stdout and modifies nothing.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_TESTS = ROOT / "apps" / "web" / "tests"
DESKTOP_TESTS = ROOT / "apps" / "desktop" / "frontend" / "tests"

# `test(` and `test.describe(` both start a spec; the count is a proxy for how
# much work the pair duplicates, not an exact runtime.
TEST_PATTERN = re.compile(r"^\s*test(?:\.(?:only|skip|fixme))?\s*\(", re.MULTILINE)
REEXPORT_PATTERN = re.compile(r"^\s*import\s+\"[^\"]*\.spec\";?\s*$", re.MULTILINE)


def spec_names(directory: Path) -> set[str]:
    return {path.stem.removesuffix(".spec") for path in directory.glob("*.spec.ts")}


def describe(directory: Path, name: str) -> tuple[str, int, int]:
    """Return (kind, test count, line count) for one spec."""
    path = directory / f"{name}.spec.ts"
    text = path.read_text(encoding="utf-8")
    # A delegating spec may still add a fixture around the import, so it counts as
    # one when the import is the only thing that declares tests.
    declares_tests = bool(TEST_PATTERN.search(text))
    imports_web_spec = bool(REEXPORT_PATTERN.search(text))
    if imports_web_spec and declares_tests:
        kind = "re-export + fixture"
    elif imports_web_spec:
        kind = "re-export"
    else:
        kind = "own file"
    return kind, len(TEST_PATTERN.findall(text)), len(text.splitlines())


def report() -> str:
    web = spec_names(WEB_TESTS)
    desktop = spec_names(DESKTOP_TESTS)
    shared = sorted(web & desktop)
    desktop_only = sorted(desktop - web)

    lines = [
        "# End-to-end spec overlap",
        "",
        f"- Web specs: {len(web)}",
        f"- Desktop specs: {len(desktop)}",
        f"- Same name on both ends: {len(shared)}",
        f"- Desktop only: {len(desktop_only)}",
        "",
        "## Same name on both ends",
        "",
        "A desktop spec that only re-exports the web file runs the identical",
        "tests through the desktop host. A spec with its own file may still",
        "overlap heavily, but it is a separate implementation and needs a diff",
        "before it can be judged.",
        "",
        "| spec | desktop kind | desktop tests | web tests | desktop lines |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in shared:
        kind, tests, size = describe(DESKTOP_TESTS, name)
        _, web_tests, _ = describe(WEB_TESTS, name)
        lines.append(f"| `{name}` | {kind} | {tests} | {web_tests} | {size} |")

    lines += [
        "",
        "## Desktop only",
        "",
        "These are the specs that exist because of the native host; trimming the",
        "shared ones must keep all of them.",
        "",
    ]
    for name in desktop_only:
        _, tests, size = describe(DESKTOP_TESTS, name)
        lines.append(f"- `{name}` ({tests} tests, {size} lines)")

    reexports = [name for name in shared if describe(DESKTOP_TESTS, name)[0] == "re-export"]
    lines += [
        "",
        "## Plain re-exports",
        "",
        f"{len(reexports)} of the {len(shared)} shared specs are desktop files that",
        "only import the web implementation. Trimming those would remove a desktop",
        "run of tests the web suite already executes; keeping one smoke subset of",
        "them keeps the host wiring covered.",
        "",
    ]
    for name in reexports:
        lines.append(f"- `{name}`")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write here instead of stdout.")
    arguments = parser.parse_args()
    text = report()
    if arguments.output:
        arguments.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
