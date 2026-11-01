"""Schema generation is deterministic and must never discover platform resources."""

import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

from wenyi_backend.application import create_app

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/export_openapi.py"


def test_offline_schema_matches_shared_routes_without_touching_ports(backend_context, monkeypatch):
    monkeypatch.setattr(
        "socket.create_connection", Mock(side_effect=AssertionError("Network used"))
    )
    monkeypatch.setattr("wenyi_backend.application.__version__", "installation-specific-version")
    module = runpy.run_path(str(SCRIPT))
    first = module["build_schema"]()
    second = module["build_schema"]()
    assert first == second
    actual = create_app(backend_context).openapi()
    actual["info"]["version"] = "0.0.0"
    assert first == actual
    assert "/projects/{pid}/pause" in first["paths"]
    assert "/projects/{pid}/review/runs" in first["paths"]
    assert not any(path.startswith("/desktop") for path in first["paths"])


def test_offline_schema_script_works_without_platform_imports(tmp_path):
    import subprocess

    # A separate interpreter also excludes platform imports made by other test modules.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; "
            f"runpy.run_path({str(SCRIPT)!r})['build_schema'](); "
            "assert not any(n.startswith(('wenyi_api', 'wenyi_desktop', 'psycopg', 'redis', 'arq')) "
            "for n in sys.modules)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
