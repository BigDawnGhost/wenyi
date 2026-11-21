"""Release-generator tests with a synthetic, offline Cargo registry."""

import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib", reason="release tooling requires Python 3.11+")
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/release/core_native_notices.py"
ARTIFACT = ROOT / "packages/core/THIRD_PARTY_NOTICES.md"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def make_crate(cache: Path, name: str, version: str, files: dict[str, str]) -> tuple[Path, str]:
    stem = f"{name}-{version}"
    path = cache / f"{stem}.crate"
    files = {
        **files,
        "Cargo.toml": f'[package]\nname = "{name}"\nversion = "{version}"\nlicense = "MIT"\n',
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            payload = text.encode("utf-8")
            member = tarfile.TarInfo(f"{stem}/{name}")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, list[Path], Callable[..., subprocess.CompletedProcess]]:
    script = tmp_path / SCRIPT.relative_to(ROOT)
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    materials = script.with_suffix(".toml")
    core = tmp_path / "packages/core"
    lock = core / "rust/Cargo.lock"
    lock.parent.mkdir(parents=True)
    output = core / "THIRD_PARTY_NOTICES.md"
    cache = tmp_path / "cargo-home/registry/cache/index.crates.io-test"
    cache.mkdir(parents=True)
    monkeypatch.setenv("CARGO_HOME", str(cache.parents[2]))

    paths = []
    entries = []
    snapshot = []
    for version, files in [
        ("1.0.0", {"license": "Primary text\nCopyright First.\n", "NOTICE": "Extra obligations"}),
        (
            "2.0.0",
            {
                "license": "Primary text\r\nCopyright Second.\r\n",
                "special/COPYRIGHT": "first\nExtra obligations  \nlast",
            },
        ),
    ]:
        path, sha = make_crate(cache, "example", version, files)
        paths.append(path)
        entries.append(
            f'[[package]]\nname = "example"\nversion = "{version}"\n'
            f'source = "{REGISTRY}"\nchecksum = "{sha}"\n'
        )
        snapshot.append(f"example {version} {sha}\n")
    lock.write_text(
        'version = 4\n[[package]]\nname = "wenyi-core-native"\nversion = "0.0.0"\n'
        + "\n".join(reversed(entries)),
        encoding="utf-8",
    )
    digest = hashlib.sha256("".join(snapshot).encode("utf-8")).hexdigest()
    materials.write_text(
        f'review_sha256 = "{digest}"\n'
        '[group.base]\nfiles = ["license"]\ncrates = ["example 1.0.0", "example 2.0.0"]\n'
        '[extra]\n"example 1.0.0" = ["NOTICE"]\n'
        '"example 2.0.0" = ["special/COPYRIGHT#L2-L2", "@upstream"]\n'
        '[supplement.upstream]\nsource = "https://example.com/LICENSE"\n'
        'text = "Third-party license text"\n[note]\n',
        encoding="utf-8",
    )

    def cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(script), *args], cwd=tmp_path, capture_output=True, text=True
        )

    return lock, materials, output, paths, cli


def test_generator_reproduces_notices_and_check_is_read_only(checkout):
    _, _, output, _, cli = checkout
    assert cli("--check").returncode == 1
    assert not output.exists()
    result = cli()
    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    assert text.index("| `example` | `1.0.0` |") < text.index("| `example` | `2.0.0` |")
    assert "Copyright First." in text and "Copyright Second." in text
    assert "Third-party license text" in text
    assert text.count("Extra obligations") == 1
    assert re.search(r"\[`NOTICE`\]\(([^)]+)\).*?\[`special/COPYRIGHT#L2-L2`\]\(\1\)", text, re.S)
    original = output.read_bytes()
    assert cli().returncode == 0
    assert output.read_bytes() == original
    assert cli("--check").returncode == 0
    output.write_bytes(b"stale")
    result = cli("--check")
    assert result.returncode == 1 and "missing or stale" in result.stderr
    assert output.read_bytes() == b"stale"


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [
        ("unknown", "unreviewed locked packages"),
        ("source", "unsupported or duplicate local package/source"),
        ("checksum", "locked archive checksums changed"),
        ("cache", "missing .crate archive"),
        ("tamper", "no cached archive matches"),
        ("file", "missing regular archive member"),
        ("excerpt", "material excerpt out of range"),
        ("supplement", "missing supplemental material"),
        ("empty", "empty or invalid notice material"),
    ],
)
def test_generator_fails_without_replacing_notices(checkout, change, expected_error):
    lock, materials, output, archives, cli = checkout
    output.write_bytes(b"last good version")
    locked = lock.read_text()
    reviewed = materials.read_text()
    if change == "unknown":
        lock.write_text(locked.replace('version = "2.0.0"', 'version = "3.0.0"'))
        materials.write_text(reviewed.replace(', "example 2.0.0"', ""))
    elif change == "source":
        lock.write_text(locked.replace(REGISTRY, "git+https://example.com/example"))
    elif change == "checksum":
        lock.write_text(re.sub(r'checksum = "[0-9a-f]+"', f'checksum = "{"0" * 64}"', locked))
    elif change == "cache":
        archives[0].unlink()
    elif change == "tamper":
        archives[0].write_bytes(b"not an authentic .crate")
    elif change == "file":
        materials.write_text(reviewed.replace('"NOTICE"', '"NOT_FOUND"'))
    elif change == "excerpt":
        materials.write_text(reviewed.replace("#L2-L2", "#L2-L20"))
    elif change == "supplement":
        materials.write_text(reviewed.replace('"@upstream"', '"@unknown"'))
    elif change == "empty":
        materials.write_text(reviewed.replace('"Third-party license text"', '""'))

    result = cli()
    assert result.returncode == 1 and expected_error in result.stderr
    assert output.read_bytes() == b"last good version"


def test_checked_in_notice_identifies_every_locked_release():
    lock = ROOT / "packages/core/rust/Cargo.lock"
    entries = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    expected = {(entry["name"], entry["version"]) for entry in entries if "source" in entry}
    text = ARTIFACT.read_text(encoding="utf-8")
    listed = re.findall(r"^\| `([^`]+)` \| `([^`]+)` \|", text, re.M)
    assert sorted(listed) == sorted(expected)
    links = set(re.findall(r"\]\(#(material-\d+)\)", text))
    headings = set(re.findall(r"^### (material-\d+)$", text, re.M))
    assert links == headings
    assert "| `lopdf` | `0.47.0` | `MIT` |" in text
    assert "Copyright (c) 2016 Junfeng Liu" in text
    assert "LLVM Exceptions to the Apache 2.0 License" in text
    assert "Copyright 2013 Google Inc." in text
    assert "Copyright © WHATWG (Apple, Google, Mozilla, Microsoft)." in text
    assert "UNICODE LICENSE V3" in text
    assert "Copyright (c) 2025 Victor Zverovich" in text
