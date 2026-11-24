"""Compile the private PyO3 extension without changing Hatch's VCS versioning."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import platform_tags


def missing_rust_tools() -> list[str]:
    """Return the Rust toolchain commands the native build needs but cannot find."""
    missing = []
    if shutil.which("cargo") is None:
        missing.append("cargo")
    # rust_std_notices honours RUSTC/CARGO_BUILD_RUSTC, so only require rustc on
    # PATH when the build would fall back to the default compiler command.
    rustc_override = os.environ.get("RUSTC") or os.environ.get("CARGO_BUILD_RUSTC")
    if rustc_override is None and shutil.which("rustc") is None:
        missing.append("rustc")
    return missing


def require_rust_toolchain() -> None:
    """Fail the build early with installation guidance instead of a raw linker error."""
    missing = missing_rust_tools()
    if not missing:
        return
    missing_text = " and ".join(missing)
    was_were = "was" if len(missing) == 1 else "were"
    guidance = (
        "Install the Rust toolchain via rustup: https://rustup.rs",
        "  - Windows: winget install Rustlang.Rustup  (or download https://win.rustup.rs)",
        "  - Linux/macOS: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        "Then open a new shell so ~/.cargo/bin joins PATH and retry, e.g. 'uv sync'.",
    )
    raise SystemExit(
        f"Building wenyi-core compiles a native Rust extension, but {missing_text} "
        f"{was_were} not found on PATH.\n" + "\n".join(guidance)
    )


def rust_std_notices() -> dict[str, str]:
    """Include the standard library's own attribution from the toolchain used to build the wheel."""
    if override := os.environ.get("WENYI_RUST_LICENSE_DIR"):
        directory = Path(override).resolve()
    else:
        compiler = os.environ.get("RUSTC") or os.environ.get("CARGO_BUILD_RUSTC") or "rustc"
        sysroot = subprocess.check_output([compiler, "--print", "sysroot"], text=True).strip()
        directory = Path(sysroot) / "share/doc/rust"
    copyright = directory / "COPYRIGHT-library.html"
    texts = sorted((directory / "licenses").glob("*.txt"))
    if not copyright.is_file() or not {"MIT.txt", "Apache-2.0.txt"} <= {p.name for p in texts}:
        raise RuntimeError(
            f"Missing Rust standard-library license materials in {directory}. Use the official "
            "Rust toolchain, or point WENYI_RUST_LICENSE_DIR at the matching toolchain's "
            "directory containing COPYRIGHT-library.html and licenses/."
        )
    return {
        str(path): f"wenyi_core/_licenses/rust-std/{path.relative_to(directory).as_posix()}"
        for path in (copyright, *texts)
    }


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        require_rust_toolchain()
        root = Path(self.root)
        rust = root / "rust"
        target = rust / "target"
        if version != "editable":
            build_data["force_include"].update(rust_std_notices())
        subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "--locked",
                "--manifest-path",
                str(rust / "Cargo.toml"),
                "--target-dir",
                str(target),
            ],
            env=dict(os.environ, PYO3_PYTHON=sys.executable, PYO3_BUILD_EXTENSION_MODULE="1"),
            check=True,
        )
        library, extension = {
            "Windows": ("_epub_native.dll", ".pyd"),
            "Darwin": ("lib_epub_native.dylib", ".abi3.so"),
        }.get(platform.system(), ("lib_epub_native.so", ".abi3.so"))
        name = "_epub_native" + extension
        binary = target / "release" / library
        module = f"wenyi_core/markup/{name}"
        if version == "editable":
            shutil.copyfile(binary, root / module)
        else:
            build_data["force_include"][str(binary)] = module
        build_data["pure_python"] = False
        # Linux release wheels are audited and retagged by cibuildwheel.
        tags = platform_tags()
        wheel_platform = (
            next(tag for tag in tags if tag.startswith("linux_"))
            if sys.platform.startswith("linux")
            else next(tags)
        )
        if sys.platform == "darwin" and (minimum := os.environ.get("MACOSX_DEPLOYMENT_TARGET")):
            # packaging.tags reports the running OS, not the linker's deployment target.
            from packaging.tags import mac_platforms

            mac_version = tuple(map(int, minimum.split(".")))
            wheel_platform = next(mac_platforms(mac_version, platform.machine()))
        build_data["tag"] = f"cp310-abi3-{wheel_platform}"
