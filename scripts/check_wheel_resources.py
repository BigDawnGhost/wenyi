"""Fail packaging validation when language/prompt resources are missing from a core wheel."""
from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    wheels = list(directory.glob("wenyi_core-*.whl"))
    if not wheels:
        raise SystemExit("No wenyi-core wheel found")
    resource_root = Path(__file__).resolve().parents[1] / "packages/core/wenyi_core/i18n/data"
    expected = {
        "wenyi_core/i18n/data/" + path.relative_to(resource_root).as_posix()
        for path in resource_root.rglob("*") if path.is_file()
    }
    if not expected:
        raise SystemExit("Source language resources are missing")
    for wheel in wheels:
        with ZipFile(wheel) as archive:
            missing = expected - set(archive.namelist())
        if missing:
            raise SystemExit(f"{wheel.name} is missing: {', '.join(sorted(missing))}")
        print(f"{wheel.name}: {len(expected)} bundled language resources verified")


if __name__ == "__main__":
    main()
