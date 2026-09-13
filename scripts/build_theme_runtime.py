from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
from pathlib import Path, PurePosixPath

DISTRIBUTION = "quickjs-ng"
VERSION = "0.16.2.1"
WRAPPER = {
    "commit": "f2ac026f5105b492b15b331bf1b71bd7a4e9cfb2",
    "url": "https://codeload.github.com/genotrance/quickjs-ng/tar.gz/f2ac026f5105b492b15b331bf1b71bd7a4e9cfb2",
    "sha256": "4a0679c607d8c27bc647ca0e61a203066e66865cc6970d93479e83c079eec760",
}
ENGINE = {
    "commit": "1ab8676f4b6d6d669baeb5f21790fb9734636a20",
    "url": "https://codeload.github.com/quickjs-ng/quickjs/tar.gz/1ab8676f4b6d6d669baeb5f21790fb9734636a20",
    "sha256": "c788fe4f65c95ecfa4055c8778e7cb221f68fcc3315686627b0856da5c38514e",
}
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 10_000
DOWNLOAD_TIMEOUT_SECONDS = 60


class SourceBuildError(RuntimeError):
    pass


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > MAX_DOWNLOAD_BYTES:
            raise SourceBuildError(f"source archive exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}")
        blob = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(blob) > MAX_DOWNLOAD_BYTES:
        raise SourceBuildError(f"source archive exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}")
    return blob


def _safe_members(archive: tarfile.TarFile) -> tuple[list[tarfile.TarInfo], PurePosixPath]:
    members: list[tarfile.TarInfo] = []
    member_types: dict[PurePosixPath, bool] = {}
    roots: set[str] = set()
    unpacked_bytes = 0
    for member in archive:
        if len(members) == MAX_MEMBERS:
            raise SourceBuildError(f"source archive exceeds {MAX_MEMBERS} members")
        if not member.name or member.name.startswith("/") or "\\" in member.name:
            raise SourceBuildError(f"unsafe archive path: {member.name!r}")
        raw_parts = member.name.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise SourceBuildError(f"unsafe archive path: {member.name!r}")
        path = PurePosixPath(member.name)
        if not (member.isfile() or member.isdir()):
            raise SourceBuildError(f"unsupported archive member: {member.name!r}")
        if path in member_types:
            raise SourceBuildError(f"duplicate archive member: {member.name!r}")
        member_types[path] = member.isdir()
        roots.add(path.parts[0])
        unpacked_bytes += member.size if member.isfile() else 0
        if unpacked_bytes > MAX_UNPACKED_BYTES:
            raise SourceBuildError(f"source archive exceeds {MAX_UNPACKED_BYTES} unpacked bytes")
        members.append(member)
    for path in member_types:
        if any(parent in member_types and not member_types[parent] for parent in path.parents):
            raise SourceBuildError(f"archive member has a file parent: {str(path)!r}")
    if len(roots) != 1:
        raise SourceBuildError("source archive must contain exactly one root directory")
    root = PurePosixPath(next(iter(roots)))
    root_members = [member for member in members if PurePosixPath(member.name) == root]
    if len(root_members) != 1 or not root_members[0].isdir():
        raise SourceBuildError("source archive root must be an explicit directory")
    return members, root


def unpack_source(blob: bytes, sha256: str, destination: Path) -> Path:
    if len(blob) > MAX_DOWNLOAD_BYTES:
        raise SourceBuildError(f"source archive exceeds {MAX_DOWNLOAD_BYTES} bytes")
    actual = hashlib.sha256(blob).hexdigest()
    if not hmac.compare_digest(actual, sha256):
        raise SourceBuildError(f"source archive SHA256 mismatch: expected {sha256}, got {actual}")
    try:
        with tarfile.open(fileobj=BytesIO(blob), mode="r:gz") as archive:
            members, root = _safe_members(archive)
            if destination.exists():
                raise SourceBuildError(f"extraction destination already exists: {destination}")
            destination.mkdir(parents=True)
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise SourceBuildError(f"cannot read archive member: {member.name!r}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if target.stat().st_size != member.size:
                    raise SourceBuildError(f"archive member size mismatch: {member.name!r}")
    except tarfile.TarError as error:
        raise SourceBuildError(f"invalid source archive: {error}") from error
    return destination / root


def _assemble_source(wrapper_root: Path, engine_root: Path) -> tuple[bytes, bytes]:
    pyproject = wrapper_root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    old_version = 'version = "0.12.1.1"'
    if text.count('name = "quickjs-ng"') != 1 or text.count(old_version) != 1:
        raise SourceBuildError("wrapper project metadata differs from the pinned source")
    pyproject.write_text(text.replace(old_version, f'version = "{VERSION}"'), encoding="utf-8")

    upstream = wrapper_root / "upstream-quickjs"
    if not upstream.is_dir() or any(upstream.iterdir()):
        raise SourceBuildError("wrapper upstream-quickjs is not the expected empty submodule")
    wrapper_license = (wrapper_root / "LICENSE").read_bytes()
    engine_license = (engine_root / "LICENSE").read_bytes()
    shutil.rmtree(upstream)
    shutil.move(str(engine_root), upstream)
    (wrapper_root / "LICENSE.quickjs").write_bytes(engine_license)
    return wrapper_license, engine_license


def _quickjs_wheels(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.lower().startswith("quickjs_ng-")
        and path.suffix.lower() == ".whl"
    )


def _verify_wheel(wheel: Path, wrapper_license: bytes, engine_license: bytes) -> None:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise SourceBuildError("built wheel does not contain exactly one METADATA file")
        metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_names[0]))
        if metadata["Name"] != DISTRIBUTION or metadata["Version"] != VERSION:
            raise SourceBuildError("built wheel distribution metadata is incorrect")
        licenses = {
            PurePosixPath(name).name: archive.read(name)
            for name in archive.namelist()
            if ".dist-info/" in name and PurePosixPath(name).name in {"LICENSE", "LICENSE.quickjs"}
        }
    if licenses.get("LICENSE") != wrapper_license:
        raise SourceBuildError("built wheel is missing the wrapper license notice")
    if licenses.get("LICENSE.quickjs") != engine_license:
        raise SourceBuildError("built wheel is missing the engine license notice")


def build(wheel_dir: Path) -> Path:
    output = wheel_dir.expanduser().resolve()
    if _quickjs_wheels(output):
        raise SourceBuildError(f"output directory already contains a quickjs-ng wheel: {output}")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quickjs-ng-source-") as temporary:
        root = Path(temporary)
        wrapper_root = unpack_source(_download(WRAPPER["url"]), WRAPPER["sha256"], root / "wrapper")
        engine_root = unpack_source(_download(ENGINE["url"]), ENGINE["sha256"], root / "engine")
        wrapper_license, engine_license = _assemble_source(wrapper_root, engine_root)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(output),
                str(wrapper_root),
            ],
            check=True,
            stdout=sys.stderr,
        )
    wheels = _quickjs_wheels(output)
    if (
        len(wheels) != 1
        or not wheels[0].name.startswith(f"quickjs_ng-{VERSION}-")
        or "-abi3-" not in wheels[0].name
    ):
        for wheel in wheels:
            wheel.unlink()
        raise SourceBuildError("pip did not produce exactly one pinned abi3 wheel")
    try:
        _verify_wheel(wheels[0], wrapper_license, engine_license)
    except (SourceBuildError, zipfile.BadZipFile):
        wheels[0].unlink()
        raise
    return wheels[0].resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the pinned quickjs-ng theme runtime wheel")
    parser.add_argument("--wheel-dir", type=Path, default=Path("build/theme-runtime-wheels"))
    args = parser.parse_args(argv)
    wheel = build(args.wheel_dir)
    print(
        json.dumps(
            {
                "distribution": DISTRIBUTION,
                "version": VERSION,
                "wrapper": WRAPPER,
                "engine": ENGINE,
                "wheel_path": str(wheel),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SourceBuildError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
