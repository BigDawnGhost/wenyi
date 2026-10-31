"""Download and unpack official plugin assets without importing plugin code."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

import httpx

REPOSITORY = "https://github.com/BigDawnGhost/wenyi-babeldoc-bridge"
MANIFEST_URL = f"{REPOSITORY}/releases/latest/download/desktop-plugin.json"
MAX_ARCHIVE = 2 * 1024**3
MAX_UNPACKED = 6 * 1024**3


async def require_https(request: httpx.Request) -> None:
    if request.url.scheme != "https":
        raise ValueError("Plugin downloads require HTTPS, including redirects")


def platform_key() -> str | None:
    system = {"win32": "windows", "darwin": "macos", "linux": "linux"}.get(sys.platform)
    arch = {"AMD64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        platform.machine()
    )
    key = f"{system}-{arch}"
    return (
        key if key in {"windows-x86_64", "linux-x86_64", "macos-x86_64", "macos-aarch64"} else None
    )


def validate_manifest(value: dict, target: str) -> dict:
    if value.get("protocol") != 1 or not re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", str(value.get("version", ""))
    ):
        raise ValueError("Unsupported plugin manifest")
    version = value["version"]
    runtime = value["platforms"][target]
    models = value["models"]
    for item in (runtime, models):
        name = item["name"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", name):
            raise ValueError("Invalid plugin asset name")
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("Invalid plugin checksum")
        if type(item["size"]) is not int or not 0 < item["size"] <= MAX_ARCHIVE:
            raise ValueError("Invalid plugin size")
    return {"protocol": 1, "version": version, "runtime": runtime, "models": models}


async def read_manifest(client: httpx.AsyncClient, target: str) -> dict:
    async with client.stream("GET", MANIFEST_URL) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 256 * 1024:
                raise ValueError("Plugin manifest is too large")
    return validate_manifest(json.loads(data), target)


async def download(
    client: httpx.AsyncClient,
    version: str,
    asset: dict,
    destination: Path,
    progress: Callable[[int, int], None],
) -> None:
    """A verified archive is reusable on retry; partial archives are never installed."""
    if destination.is_file():
        if (
            destination.stat().st_size == asset["size"]
            and await asyncio.to_thread(sha256, destination) == asset["sha256"]
        ):
            progress(asset["size"], asset["size"])
            return
    temporary = destination.with_suffix(".part")
    digest = hashlib.sha256()
    count = 0
    progress(0, asset["size"])
    try:
        url = f"{REPOSITORY}/releases/download/v{version}/{asset['name']}"
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with temporary.open("wb") as stream:
                async for chunk in response.aiter_bytes(256 * 1024):
                    count += len(chunk)
                    if count > asset["size"]:
                        raise ValueError("Plugin download exceeds its declared size")
                    stream.write(chunk)
                    digest.update(chunk)
                    progress(count, asset["size"])
        if count != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise ValueError("Plugin download checksum mismatch")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unpack(archive: Path, destination: Path) -> None:
    """Only regular files/directories, bounded expansion, no links or path aliases."""
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 100_000 or sum(item.file_size for item in entries) > MAX_UNPACKED:
            raise ValueError("Plugin archive is too large")
        seen: set[str] = set()
        for item in entries:
            name = item.filename.rstrip("/")
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            if (
                not name
                # ZIP normalizes backslashes on Windows and truncates NUL bytes.
                or item.orig_filename != item.filename
                or path.is_absolute()
                or str(path) != name
                or any(
                    part in {"", ".", ".."}
                    or ":" in part
                    or "\\" in part
                    or part.endswith((".", " "))
                    for part in path.parts
                )
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                or name.casefold() in seen
            ):
                raise ValueError("Unsafe plugin archive entry")
            seen.add(name.casefold())
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.is_dir():
                target.mkdir(exist_ok=True)
                continue
            with bundle.open(item) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            target.chmod(0o755 if mode & 0o111 else 0o644)
