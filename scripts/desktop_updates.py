"""Collect signed Tauri v2 updates and publish a complete stable manifest offline."""

from __future__ import annotations

import argparse
import base64
import filecmp
import json
import plistlib
import re
import shutil
import tarfile
from pathlib import Path
from urllib.parse import quote

PLATFORMS = {
    "windows-x64": ("windows-x86_64", "nsis", "{name}_{version}_x64-setup.exe", ".exe"),
    "linux-x64": ("linux-x86_64", "appimage", "{name}_{version}_amd64.AppImage", ".AppImage"),
    "macos-arm64": ("darwin-aarch64", "macos", "{name}.app.tar.gz", ".app.tar.gz"),
}


def stable_version(version: str) -> None:
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("Updater manifests require a stable MAJOR.MINOR.PATCH version")


def signature(path: Path, version: str | None = None) -> str:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing updater signature: {path}")
    value = path.read_text(encoding="utf-8").strip()
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        lines = decoded.splitlines()
        if len(lines) != 4 or not lines[0].startswith("untrusted comment:"):
            raise ValueError("Invalid minisign structure")
        if not lines[2].startswith("trusted comment:"):
            raise ValueError("Missing trusted comment")
        if version is not None:
            signed_versions = [
                field.removeprefix("version:")
                for field in lines[2].removeprefix("trusted comment: ").split("\t")
                if field.startswith("version:")
            ]
            if signed_versions != [version]:
                raise ValueError("Updater signature must bind the current release version")
        packet = base64.b64decode(lines[1], validate=True)
        global_signature = base64.b64decode(lines[3], validate=True)
        if len(packet) != 74 or packet[:2] not in (b"Ed", b"ED"):
            raise ValueError("Invalid minisign packet")
        if len(global_signature) != 64:
            raise ValueError("Invalid global signature")
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Malformed Tauri updater signature: {path}") from error
    return value


def macos_identity(artifact: Path, version: str) -> None:
    """The Tauri macOS updater filename has no version; inspect its embedded identity."""
    try:
        with tarfile.open(artifact, "r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.name.endswith(".app/Contents/Info.plist")
            ]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 1024 * 1024:
                raise ValueError("Expected one application Info.plist")
            with archive.extractfile(members[0]) as stream:
                identity = plistlib.load(stream)
            if identity.get("CFBundleShortVersionString") != version:
                raise ValueError("macOS updater application version differs from release")
    except (tarfile.TarError, OSError, plistlib.InvalidFileException) as error:
        raise ValueError(f"Invalid macOS updater archive: {artifact}") from error


def collect(source: Path, output: Path, version: str, platform: str, name: str) -> None:
    stable_version(version)
    _, directory, template, extension = PLATFORMS[platform]
    artifact = source / directory / template.format(name=name, version=version)
    if not artifact.is_file() or artifact.is_symlink() or not artifact.stat().st_size:
        raise ValueError(f"Missing or empty updater artifact: {artifact}")
    signed = Path(str(artifact) + ".sig")
    signature(signed, version)
    if platform == "macos-arm64":
        macos_identity(artifact, version)
    target = output / f"wenyi-desktop-{version}-{platform}{extension}"
    # The installer collector already copied Windows/Linux's identical payload.
    if target.exists() and (
        target.is_symlink() or not filecmp.cmp(target, artifact, shallow=False)
    ):
        raise ValueError(f"Existing updater payload differs: {target}")
    target_signature = Path(str(target) + ".sig")
    if target_signature.exists():
        raise ValueError(f"Refusing to overwrite: {target_signature}")
    output.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(artifact, target)
    shutil.copy2(signed, target_signature)


def manifest(
    assets: Path, version: str, tag: str, repository: str, notes: str | None = None
) -> dict:
    stable_version(version)
    tagged = tag.removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", tagged):
        raise ValueError("Release tag must match the current stable version")
    normalized_tag = ".".join(str(part) for part in (*map(int, tagged.split(".")), 0, 0)[:3])
    if normalized_tag != version:
        raise ValueError("Release tag must match the current stable version")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected a GitHub owner/repository")
    platforms = {}
    for platform, (key, _, _, extension) in PLATFORMS.items():
        filename = f"wenyi-desktop-{version}-{platform}{extension}"
        artifact = assets / filename
        if not artifact.is_file() or artifact.is_symlink() or not artifact.stat().st_size:
            raise ValueError(f"Missing or empty updater artifact: {artifact}")
        if platform == "macos-arm64":
            macos_identity(artifact, version)
        platforms[key] = {
            "signature": signature(assets / f"{filename}.sig", version),
            "url": (
                f"https://github.com/{repository}/releases/download/"
                f"{quote(tag, safe='')}/{quote(filename, safe='')}"
            ),
        }
    result = {"version": version, "platforms": platforms}
    if notes is not None:
        result["notes"] = notes
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "manifest"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("release-assets"))
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--tag")
    parser.add_argument("--repository", default="BigDawnGhost/wenyi")
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args()
    if args.command == "collect":
        if not args.platform:
            parser.error("collect requires --platform")
        config = Path(__file__).resolve().parents[1] / "apps/desktop/tauri.conf.json"
        name = json.loads(config.read_text(encoding="utf-8"))["productName"]
        collect(args.source, args.output, args.version, args.platform, name)
    else:
        notes = args.notes_file.read_text(encoding="utf-8") if args.notes_file else None
        result = manifest(args.source, args.version, args.tag or "", args.repository, notes)
        target = args.output / "latest.json"
        args.output.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")


if __name__ == "__main__":
    main()
