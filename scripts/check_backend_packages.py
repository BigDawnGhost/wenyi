"""Validate release archives and dependency isolation in clean wheel installations.

Run after building all five workspace packages: python scripts/check_backend_packages.py dist.
No model, database, Redis, credential-store, or user-workspace access is required.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("wenyi_core", "wenyi_cli", "wenyi_backend", "wenyi_api", "wenyi_desktop")
PROFILES = {
    "cli": (
        ("wenyi_core", "wenyi_cli"),
        (
            "wenyi_backend",
            "wenyi_api",
            "wenyi_desktop",
            "psycopg",
            "psycopg_pool",
            "arq",
            "redis",
            "keyring",
        ),
    ),
    "shared": (
        ("wenyi_core", "wenyi_backend"),
        (
            "wenyi_api",
            "wenyi_desktop",
            "wenyi_cli",
            "psycopg",
            "psycopg_pool",
            "arq",
            "redis",
            "keyring",
        ),
    ),
    "web": (
        ("wenyi_core", "wenyi_backend", "wenyi_api"),
        ("wenyi_desktop", "wenyi_cli", "keyring", "PyInstaller"),
    ),
    "desktop": (
        ("wenyi_core", "wenyi_backend", "wenyi_desktop"),
        ("wenyi_api", "wenyi_cli", "psycopg", "psycopg_pool", "arq", "redis", "PyInstaller"),
    ),
}

IMPORT_SMOKE = """
import importlib
import importlib.util
import pkgutil
import sys
installed, absent = sys.argv[1].split(","), sys.argv[2].split(",")
for name in absent:
    assert importlib.util.find_spec(name) is None, f"Unexpected installed dependency: {name}"
for name in installed:
    importlib.import_module(name)
if "wenyi_backend" in installed:
    import wenyi_backend
    for module in pkgutil.walk_packages(wenyi_backend.__path__, "wenyi_backend."):
        importlib.import_module(module.name)
if "wenyi_api" in installed:
    import wenyi_api.main
    import wenyi_api.workers
    import psycopg, psycopg_pool, arq, redis
if "wenyi_desktop" in installed:
    import wenyi_desktop.main
    import wenyi_desktop.__main__
    import keyring
"""

# This tests HTTP assembly without starting the production lifespan or touching services.
# Service connectivity is covered by the separate PostgreSQL/Redis integration CI job.
WEB_HEALTH_SMOKE = """
import asyncio
import httpx
from wenyi_api.main import app
async def main():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"
asyncio.run(main())
"""


def archives(directory: Path) -> dict[str, Path]:
    wheels: dict[str, Path] = {}
    versions: set[str] = set()
    resources = ROOT / "packages/core/wenyi_core/i18n/data"
    prompts = {
        "wenyi_core/i18n/data/" + path.relative_to(resources).as_posix()
        for path in resources.rglob("*")
        if path.is_file()
    }
    assert prompts, "Core language resources are missing"
    for package in PACKAGES:
        candidates = list(directory.glob(f"{package}-*.whl"))
        assert len(candidates) == 1, f"Expected one {package} wheel in {directory}"
        wheel = wheels[package] = candidates[0]
        with ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            assert len(metadata_names) == 1, wheel
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            assert metadata["Name"].replace("-", "_") == package
            version = metadata["Version"]
            assert version and version != "0.0.0", wheel
            versions.add(version)
            assert any(name.startswith(f"{package}/") and name.endswith(".py") for name in names)
            for other in set(PACKAGES) - {package}:
                assert not any(name.startswith(f"{other}/") for name in names), (wheel, other)
            if package == "wenyi_core":
                assert prompts <= names, f"Missing wheel prompts: {prompts - names}"
        sdist = directory / f"{package}-{version}.tar.gz"
        assert sdist.is_file(), f"Missing sdist: {sdist}"
        with tarfile.open(sdist) as archive:
            prefix = f"{package}-{version}/"
            info = archive.extractfile(prefix + "PKG-INFO")
            assert info is not None
            assert BytesParser().parsebytes(info.read())["Version"] == version
            names = {name.removeprefix(prefix) for name in archive.getnames()}
            assert "pyproject.toml" in names
            assert any(name.startswith(f"{package}/") and name.endswith(".py") for name in names)
            if package == "wenyi_core":
                assert prompts <= names, f"Missing sdist prompts: {prompts - names}"
        print(f"{package}: wheel/sdist version {version} verified", flush=True)
    assert len(versions) == 1, f"Workspace release versions disagree: {versions}"
    return wheels


def installations(wheels: dict[str, Path]) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "WENYI_", "UV_PROJECT_ENVIRONMENT"))
        and key not in {"DATABASE_URL", "REDIS_URL", "DATA_DIR", "VIRTUAL_ENV"}
    }
    for profile, (installed, absent) in PROFILES.items():
        with tempfile.TemporaryDirectory(prefix=f"wenyi-{profile}-wheel-") as temporary:
            root = Path(temporary)
            environment = root / "venv"
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run(
                ["uv", "venv", "--python", sys.executable, str(environment)], check=True, env=env
            )
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    *map(str, (wheels[p] for p in installed)),
                ],
                check=True,
                env=env,
            )
            subprocess.run(
                [str(python), "-I", "-c", IMPORT_SMOKE, ",".join(installed), ",".join(absent)],
                check=True,
                cwd=root,
                env=env,
                timeout=60,
            )
            if profile == "web":
                subprocess.run(
                    [str(python), "-I", "-c", WEB_HEALTH_SMOKE],
                    check=True,
                    cwd=root,
                    env=env,
                    timeout=30,
                )
            if profile in {"cli", "desktop"}:
                subprocess.run(
                    [str(python), "-I", "-m", f"wenyi_{profile}", "--help"],
                    check=True,
                    cwd=root,
                    env=env,
                    timeout=30,
                )
            print(f"{profile}: clean wheel dependency isolation verified", flush=True)


def main() -> None:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    installations(archives(directory))


if __name__ == "__main__":
    main()
