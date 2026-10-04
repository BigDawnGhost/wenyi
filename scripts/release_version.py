"""Resolve release identity from the same setuptools-scm defaults as hatch-vcs.

No version override is exported to Python packaging: hatch-vcs remains authoritative.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from packaging.version import Version
from setuptools_scm import get_version

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def normalize(value: str) -> dict[str, str]:
    """Convert supported PEP 440 identities without disguising development builds."""
    version = Version(value)
    if version.epoch or version.post is not None or len(version.release) > 3:
        raise ValueError(
            "Packaging supports MAJOR.MINOR.PATCH with optional a/b/rc/dev, not epochs/post"
        )
    major, minor, patch = (*version.release, 0, 0)[:3]
    if major > 255 or minor > 255 or patch > 65535:
        raise ValueError("Version exceeds Windows installer limits (255.255.65535)")
    base = f"{major}.{minor}.{patch}"
    prerelease = []
    if version.pre:
        label, number = version.pre
        prerelease.extend([{"a": "alpha", "b": "beta", "rc": "rc"}[label], str(number)])
    if version.dev is not None:
        prerelease.extend(["dev", str(version.dev)])
    # A dirty exact tag can be represented by SCM solely with local metadata.
    if version.local and not prerelease:
        prerelease = ["dev", "0"]
    semver = base + ("-" + ".".join(prerelease) if prerelease else "")
    if version.local:
        semver += "+" + version.local
    return {"python": str(version), "version": semver, "bundle_version": base}


def resolve(root: Path = ROOT, tag: str = "") -> dict[str, str]:
    for key in os.environ:
        if key.startswith(("SETUPTOOLS_SCM_PRETEND_", "HATCH_VCS_PRETEND_")):
            raise ValueError(f"Version override is not allowed: {key}")
    head = git(root, "rev-parse", "HEAD")
    if tag:
        if tag.startswith("-"):
            raise ValueError("Invalid tag")
        commit = git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
        if commit != head:
            raise ValueError(f"Tag {tag!r} does not point at the checkout")
        if git(root, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Explicit tag builds require a clean checkout")
    python_version = get_version(root=str(root))
    result = normalize(python_version)
    if tag:
        tagged_version = Version(tag.removeprefix("v"))
        if tagged_version != Version(python_version):
            raise ValueError("Requested tag and hatch-vcs version disagree")
        if tagged_version.dev is not None or tagged_version.local:
            raise ValueError("Release tags must not contain development/local versions")
    result.update(tag=tag, commit=head)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=os.environ.get("WENYI_BUILD_TAG", ""))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    result = resolve(tag=args.tag)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
