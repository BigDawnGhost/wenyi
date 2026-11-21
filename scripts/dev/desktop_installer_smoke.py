"""Exercise the packaged installer on a disposable Windows GitHub-hosted runner."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    if os.name != "nt" or os.getenv("RUNNER_ENVIRONMENT") != "github-hosted":
        raise SystemExit("Use a fresh GitHub-hosted Windows runner only.")
    import msvcrt
    import winreg

    product = "Wenyi Desktop"
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{product}"

    def location(hive: int) -> Path | None:
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as reg:
                return Path(winreg.QueryValueEx(reg, "InstallLocation")[0].strip('"'))
        except FileNotFoundError:
            return None

    def run(exe: Path, *args: str, expected: int = 0) -> None:
        # NSIS /D= and _?= consume the unquoted remainder, including spaces.
        command = " ".join([f'"{exe}"', *args])
        code = subprocess.run(command, executable=str(exe), timeout=240).returncode
        if code != expected:
            detail = ""
            if 100_000 <= code < 180_000:
                stage, win32_error = divmod(code - 100_000, 10_000)
                detail = f" (restricted-token stage {stage}, Win32 error {win32_error})"
            else:
                reason = {
                    8: "WebView2 setup failed",
                    9: "Restart Manager could not verify the installed files",
                    10: "setup could not acquire the installation lock",
                    11: "could not resolve the absolute installation path",
                    12: "expanded installation path exceeds NSIS capacity",
                    13: "could not find an existing ancestor when normalizing a path",
                    14: "installation path overlaps a protected or data directory",
                    15: "installation path has a reparse-point ancestor",
                    16: "installation path is a drive, share, or user profile root",
                    17: "unregistered installation path is not empty",
                    18: "installation path is not writable",
                }.get(code)
                if reason:
                    detail = f" ({reason})"
            raise AssertionError(f"{exe.name} {args!r}: expected {expected}, got {code}{detail}")

    (setup,) = Path("target/release/bundle/nsis").resolve().glob("*-setup.exe")
    machine = Path(os.environ["ProgramFiles"]) / product
    root = Path(os.environ["LOCALAPPDATA"]) / "Wenyi/Desktop"
    cu, lm = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    if any((location(cu), location(lm), root.exists(), machine.exists())):
        raise SystemExit("Refusing to use an existing Wenyi installation or workspace.")

    with tempfile.TemporaryDirectory() as temp:
        user = Path(temp).resolve() / "Current User"
        run(setup, "/S", "/NS", f"/D={user}")
        assert location(cu) == user and location(lm) is None
        run(setup, "/AllUsers", "/S", "/NS")
        assert location(cu) == user and location(lm) == machine
        run(setup, "/S", "/P", "/UPDATE", "/NS", f"/D={user}")
        run(setup, "/S", "/UPDATE", "/NS", f"/D={temp}", expected=2)
        assert location(cu) == user and location(lm) == machine

        # A lease held by another program using the same profile prevents *all*
        # selected deletions, including the first one (the catalog).
        (root / "locks").mkdir(parents=True)
        (root / "projects").mkdir()
        catalog = root / "catalog.sqlite3"
        catalog.write_bytes(b"fixture")
        app = user / "wenyi-desktop.exe"
        with (root / "locks/.desktop-profile.lock").open("w+b") as lease:
            msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            run(app, "--uninstall-user-data", "both", expected=3)
        assert catalog.read_bytes() == b"fixture" and (root / "projects").is_dir()
        run(app, "--uninstall-user-data", "projects")
        assert catalog.exists() and not (root / "projects").exists()

        run(machine / "uninstall.exe", "/S", f"_?={machine}")
        assert location(lm) is None and location(cu) == user and app.exists()
        run(user / "uninstall.exe", "/S", f"_?={user}")
        assert location(cu) is None and not app.exists() and catalog.exists()


if __name__ == "__main__":
    main()
