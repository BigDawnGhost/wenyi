"""Verify the license materials in release artifacts, plus Core's native module and data."""

from __future__ import annotations

import sys
import tarfile
from collections.abc import Callable
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


def verify_licenses(
    package: str, metadata: bytes, read_license: Callable[[str], bytes], root: Path
) -> None:
    headers = BytesParser().parsebytes(metadata, headersonly=True)
    if headers.get_all("Name") != [package] or headers.get_all("License-Expression") != [
        "GPL-3.0-only"
    ]:
        raise ValueError(f"{package}: expected first-party License-Expression: GPL-3.0-only")
    expected = {"LICENSE": root / "LICENSE"}
    if package == "wenyi-core":
        expected["THIRD_PARTY_NOTICES.md"] = root / "packages/core/THIRD_PARTY_NOTICES.md"
    for name, path in expected.items():
        if name not in headers.get_all("License-File", []):
            raise ValueError(f"{package}: missing License-File: {name}")
        # Presence of a filename, an SPDX list or a link is not a substitute for the texts.
        if read_license(name) != path.read_bytes():
            raise ValueError(f"{package}: incorrect contents of {name}")


def verify_sdist(path: Path, package: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        prefix = path.name.removesuffix(".tar.gz")

        def read(name: str) -> bytes:
            member = archive.getmember(f"{prefix}/{name}")
            stream = archive.extractfile(member) if member.isfile() else None
            if stream is None:
                raise ValueError(f"{path.name}: expected a regular file at {name}")
            with stream:
                return stream.read()

        verify_licenses(package, read("PKG-INFO"), read, ROOT)


def verify_wheel(wheel: Path, package: str) -> None:
    resource_root = ROOT / "packages/core/wenyi_core/i18n/data"
    expected = {
        "wenyi_core/i18n/data/" + path.relative_to(resource_root).as_posix()
        for path in resource_root.rglob("*")
        if path.is_file()
    }
    with ZipFile(wheel) as archive:
        members = set(archive.namelist())
        (metadata_path,) = (name for name in members if name.endswith(".dist-info/METADATA"))
        directory = metadata_path.removesuffix("/METADATA")
        verify_licenses(
            package,
            archive.read(metadata_path),
            lambda n: archive.read(f"{directory}/licenses/{n}"),
            ROOT,
        )
        if package != "wenyi-core":
            return
        if not expected or not expected <= members:
            raise ValueError(f"{wheel.name}: missing Core language resources")
        native = "wenyi_core/markup/_epub_native"
        known = {native + ".abi3.so", native + ".pyd"} & members
        if len(known) != 1:
            raise ValueError(f"{wheel.name} is missing the EPUB extension")
        for name in members - known:
            basename = PurePosixPath(name).name.lower()
            if basename.endswith((".so", ".dll", ".pyd", ".dylib")) or ".so." in basename:
                raise ValueError(
                    f"{wheel.name}: review license materials for bundled library {name}"
                )
        for name in ("COPYRIGHT-library.html", "licenses/MIT.txt", "licenses/Apache-2.0.txt"):
            if not archive.read(f"wenyi_core/_licenses/rust-std/{name}").strip():
                raise ValueError(f"{wheel.name}: missing Rust standard library notices")
        if "-cp310-abi3-" not in wheel.name:
            raise ValueError(f"{wheel.name} does not declare the CPython 3.10 stable ABI")


def main() -> None:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    if not any(directory.glob("wenyi_core-*.whl")):
        raise SystemExit("No wenyi-core wheel found")
    for package in ("wenyi-core", "wenyi-cli"):
        prefix = package.replace("-", "_")
        for pattern, verify in (("*.whl", verify_wheel), ("*.tar.gz", verify_sdist)):
            for artifact in sorted(directory.glob(f"{prefix}-{pattern}")):
                try:
                    verify(artifact, package)
                except (KeyError, OSError, ValueError) as error:
                    raise SystemExit(f"{artifact.name}: {error}") from error
                print(f"{artifact.name}: release contents verified")


if __name__ == "__main__":
    main()
