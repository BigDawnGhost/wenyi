"""Create component-scoped release assets without wrapping desktop installers."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path

INSTALLERS = {
    "linux-x64": (
        ("appimage", "{name}_{version}_amd64.AppImage"),
        ("deb", "{name}_{version}_amd64.deb"),
        ("rpm", "{name}-{version}-1.x86_64.rpm"),
    ),
    "windows-x64": (("nsis", "{name}_{version}_x64-setup.exe"),),
    "macos-arm64": (("dmg", "{name}_{version}_aarch64.dmg"),),
}


def require_output_names(destination: Path, names: set[str]) -> None:
    if destination.exists() and any(path.name not in names for path in destination.iterdir()):
        raise ValueError("Output directory contains unrelated assets; choose a fresh directory")


def cli_archive(binary: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    info = zipfile.ZipInfo(binary.name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o755) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    info.file_size = binary.stat().st_size
    with zipfile.ZipFile(destination, "w") as archive:
        with binary.open("rb") as source, archive.open(info, "w") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)


def desktop_assets(
    source: Path, destination: Path, version: str, platform: str, product_name: str
) -> list[Path]:
    """Require this version's complete platform bundle; leave old outputs untouched."""
    if platform not in INSTALLERS:
        raise ValueError(f"Unsupported Desktop platform: {platform}")
    if not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version
    ):
        raise ValueError("Expected a native semantic version")
    if not product_name or any(character in product_name for character in "/\\\0"):
        raise ValueError("Invalid product name")
    assets = [
        source / directory / template.format(name=product_name, version=version)
        for directory, template in INSTALLERS[platform]
    ]
    missing = [
        str(path.relative_to(source)) for path in assets if not path.is_file() or path.is_symlink()
    ]
    if missing:
        raise ValueError(f"Missing {platform} {version} installers: {', '.join(missing)}")
    stem = f"wenyi-desktop-{version}-{platform}"
    require_output_names(destination, {f"{stem}{asset.suffix}" for asset in assets})
    destination.mkdir(parents=True, exist_ok=True)
    outputs = []
    for asset in assets:
        target = destination / f"{stem}{asset.suffix}"
        shutil.copy2(asset, target)
        outputs.append(target)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=["cli", "desktop"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("release-assets"))
    args = parser.parse_args()
    stem = f"wenyi-{args.component}-{args.version}-{args.platform}"
    if args.component == "cli":
        require_output_names(args.output, {f"{stem}.zip"})
        cli_archive(args.source, args.output / f"{stem}.zip")
    else:
        config = Path(__file__).resolve().parents[1] / "apps/desktop/tauri.conf.json"
        product_name = json.loads(config.read_text(encoding="utf-8"))["productName"]
        desktop_assets(args.source, args.output, args.version, args.platform, product_name)


if __name__ == "__main__":
    main()
