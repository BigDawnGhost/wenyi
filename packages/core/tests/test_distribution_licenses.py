"""First-party declarations and distributable license files must agree across hosts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomli

ROOT = Path(__file__).resolve().parents[3]
LICENSE = "GPL-3.0-only"
pytestmark = pytest.mark.packaging


def metadata(path: Path) -> dict:
    return tomli.loads(path.read_text(encoding="utf-8"))


def test_workspace_packages_declare_wenyi_license():
    workspace = metadata(ROOT / "pyproject.toml")
    for member in [".", *workspace["tool"]["uv"]["workspace"]["members"]]:
        project = metadata(ROOT / member / "pyproject.toml")["project"]
        assert project.get("license") == LICENSE, member
        assert "LICENSE" in project["license-files"], member
        assert (ROOT / member / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes(), member
    assert (
        "THIRD_PARTY_NOTICES.md"
        in metadata(ROOT / "packages/core/pyproject.toml")["project"]["license-files"]
    )
    for member in [
        ".",
        "apps/desktop/frontend",
        "apps/web",
        "packages/ui",
        "packages/shared-schema",
    ]:
        assert json.loads((ROOT / member / "package.json").read_text())["license"] == LICENSE


def test_native_packages_declare_wenyi_license():
    workspace = metadata(ROOT / "Cargo.toml")["workspace"]
    for member in [*workspace["members"], "packages/core/rust"]:
        license_ = metadata(ROOT / member / "Cargo.toml")["package"].get("license")
        if license_ == {"workspace": True}:
            license_ = workspace["package"]["license"]
        assert license_ == LICENSE, member


def test_desktop_installer_carries_readable_license_materials():
    desktop = ROOT / "apps/desktop"
    resources = json.loads((desktop / "tauri.bundle.conf.json").read_text())["bundle"]["resources"]
    assert {
        destination: (desktop / source).resolve()
        for source, destination in resources.items()
        if destination.startswith("licenses/")
    } == {
        "licenses/LICENSE": ROOT / "LICENSE",
        "licenses/core/THIRD_PARTY_NOTICES.md": ROOT / "packages/core/THIRD_PARTY_NOTICES.md",
    }
