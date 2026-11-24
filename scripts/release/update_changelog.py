"""Keep CHANGELOG.md in sync with Conventional Commits.

Summarize the commits since the latest stable tag into the Unreleased section
of CHANGELOG.md. The script only reads the repository with Git; it never
creates commits, tags or pushes. At release time, run it with ``--version`` to
turn the Unreleased section into the versioned entry, then draft the bilingual
GitHub Release description with the release-notes skill.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILE = ROOT / "CHANGELOG.md"
SECTION_TITLE = "Unreleased"

HEADER = """# Changelog

All notable changes to Wenyi are summarized here, newest release first. Entries
group merged Conventional Commits; keep the Unreleased section current with

    uv run --no-sync python scripts/release/update_changelog.py

When tagging a release, convert that section with ``--version <tag>`` and draft
the bilingual GitHub Release description with the release-notes skill.
Historical releases remain available in the repository's GitHub Releases pages.
"""

COMMIT_PATTERN = re.compile(
    r"^(?P<type>feat|fix|perf|revert|docs|refactor|test|build|ci|chore|style)"
    r"(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s+(?P<subject>\S.*)$",
    re.IGNORECASE,
)
VERSION_PATTERN = re.compile(r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?$")
TYPE_GROUP = {
    "feat": "Features",
    "fix": "Bug fixes",
    "perf": "Performance improvements",
    "revert": "Reverts",
    "docs": "Documentation",
}
INTERNAL_TYPES = {"refactor", "test", "build", "ci", "chore", "style"}
GROUP_ORDER = [
    "Breaking changes",
    "Features",
    "Bug fixes",
    "Performance improvements",
    "Reverts",
    "Documentation",
    "Internal changes",
    "Other changes",
]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def git_ok(root: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return result.returncode == 0


def latest_stable_tag(root: Path) -> str | None:
    """Return the highest stable version tag reachable from HEAD, if any."""
    tags: list[tuple[tuple[int, int, int], str]] = []
    for tag in git(root, "tag", "--list").splitlines():
        tag = tag.strip()
        match = VERSION_PATTERN.match(tag)
        if not match:
            continue
        key = (int(match["major"]), int(match["minor"] or 0), int(match["patch"] or 0))
        tags.append((key, tag))
    tags.sort(reverse=True)
    for _, tag in tags:
        if git_ok(root, "merge-base", "--is-ancestor", tag, "HEAD"):
            return tag
    return None


def collect_commits(root: Path, base: str) -> list[tuple[str, str, str]]:
    raw = git(root, "log", "--no-merges", "--pretty=format:%x1e%H%x1f%s%x1f%b", f"{base}..HEAD")
    commits: list[tuple[str, str, str]] = []
    for record in raw.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 3 or not fields[0].strip():
            continue
        commits.append((fields[0].strip(), fields[1].strip(), fields[2].strip()))
    return commits


def classify(commits: list[tuple[str, str, str]]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {name: [] for name in GROUP_ORDER}
    seen: set[str] = set()
    for _, subject, body in commits:
        match = COMMIT_PATTERN.match(subject)
        breaking = bool(match and match["breaking"])
        breaking = breaking or "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body
        if match:
            kind = match["type"].lower()
            scope = (match["scope"] or "").strip()
            text = f"{scope}: {match['subject']}" if scope else match["subject"]
        else:
            kind = ""
            text = subject
        if text in seen:
            continue
        seen.add(text)
        if breaking:
            buckets["Breaking changes"].append(text)
        elif kind:
            if kind in TYPE_GROUP:
                group = TYPE_GROUP[kind]
            elif kind in INTERNAL_TYPES:
                group = "Internal changes"
            else:
                group = "Other changes"
            buckets[group].append(text)
        else:
            buckets["Other changes"].append(text)
    return buckets


def render_section(heading: str, buckets: dict[str, list[str]]) -> str:
    lines = [f"## {heading}"]
    for name in GROUP_ORDER:
        items = buckets.get(name) or []
        if not items:
            continue
        lines.append(f"- {name}")
        lines.extend(f"  - {item}" for item in items)
    if len(lines) == 1:
        lines.append("_No changes recorded._")
    return "\n".join(lines)


def split_document(text: str) -> tuple[str, list[list[str]]]:
    """Split the document into its header and its ``## `` sections."""
    header: list[str] = []
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            current = [line]
            sections.append(current)
        elif current is None:
            header.append(line)
        else:
            current.append(line)
    return "\n".join(header).strip("\n"), sections


def build_document(header: str, sections: list[list[str]]) -> str:
    parts = [header] if header else []
    parts.extend("\n".join(section).strip("\n") for section in sections)
    return "\n\n".join(parts) + "\n"


def insert_position(sections: list[list[str]]) -> int:
    for index, section in enumerate(sections):
        if section and section[0].startswith("## "):
            return index
    return len(sections)


def upsert_unreleased(sections: list[list[str]], generated: str) -> list[list[str]]:
    section_lines = generated.split("\n")
    result: list[list[str]] = []
    replaced = False
    for section in sections:
        if section and section[0].strip() == f"## {SECTION_TITLE}":
            if not replaced:
                result.append(section_lines)
                replaced = True
        else:
            result.append(section)
    if not replaced:
        result.insert(insert_position(result), section_lines)
    return result


def cut_release(sections: list[list[str]], version: str, date: str) -> list[list[str]]:
    result: list[list[str]] = []
    versioned: list[str] | None = None
    for section in sections:
        if section and section[0].strip() == f"## {SECTION_TITLE}":
            versioned = [f"## {version} - {date}", *section[1:]]
        else:
            result.append(section)
    if versioned is None:
        versioned = [f"## {version} - {date}", "_No changes recorded._"]
    position = insert_position(result)
    result.insert(position, versioned)
    result.insert(position, [f"## {SECTION_TITLE}", "_No changes recorded._"])
    return result


def extract_unreleased(text: str) -> list[str] | None:
    _, sections = split_document(text)
    for section in sections:
        if section and section[0].strip() == f"## {SECTION_TITLE}":
            return [line.rstrip() for line in section]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Conventional Commits into CHANGELOG.md")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="changelog path")
    parser.add_argument(
        "--base", help="base tag; defaults to the latest stable tag reachable from HEAD"
    )
    parser.add_argument("--version", help="turn the Unreleased section into this version entry")
    parser.add_argument("--date", help="release date for --version (default: today, UTC)")
    parser.add_argument("--check", action="store_true", help="exit 1 when Unreleased is stale")
    args = parser.parse_args()

    base = args.base or latest_stable_tag(ROOT)
    if base is None:
        print("No stable version tag found; pass --base explicitly", file=sys.stderr)
        return 1
    buckets = classify(collect_commits(ROOT, base))
    generated = render_section(SECTION_TITLE, buckets)
    path: Path = args.file
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    header, sections = split_document(current) if current else (HEADER.strip("\n"), [])
    if args.version:
        date = args.date or datetime.now(timezone.utc).date().isoformat()
        sections = cut_release(sections, args.version, date)
    else:
        sections = upsert_unreleased(sections, generated)
    if args.check:
        expected = extract_unreleased(build_document(header, sections))
        if extract_unreleased(current) != expected:
            print(
                f"CHANGELOG Unreleased section is stale (base {base}); rerun update_changelog.py",
                file=sys.stderr,
            )
            return 1
        print(f"CHANGELOG is up to date (base {base})")
        return 0
    path.write_text(build_document(header, sections), encoding="utf-8", newline="\n")
    total = sum(len(items) for items in buckets.values())
    print(f"Updated {path.name}: {total} commits since {base} summarized into {SECTION_TITLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
