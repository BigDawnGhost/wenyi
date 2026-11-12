"""Schema generation is deterministic and must never discover platform resources."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

from wenyi_backend.application import create_app

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts/contract"
SCRIPT = SCRIPTS / "export_openapi.py"
DESKTOP_SCRIPT = SCRIPTS / "export_desktop_openapi.py"


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


def _run_desktop_script():
    """Run the Desktop contract script in its own interpreter, as CI does."""
    result = subprocess.run(
        [sys.executable, str(DESKTOP_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_desktop_schema_is_generated_offline_and_deterministically():
    # The Desktop frontend imports its credential types from this document, so
    # it has to build without a workspace, a credential store or a server, and
    # it has to come out byte-identical -- otherwise the CI staleness check is
    # measuring the environment instead of the contract.
    first = _run_desktop_script()
    assert first == _run_desktop_script()
    schema = json.loads(first)
    assert "/desktop/credentials" in schema["paths"]
    assert "/desktop/projects/{pid}/exports/{export_id}/content" in schema["paths"]
    assert {"CredentialStatus", "MinerUCredentialStatus"} <= set(schema["components"]["schemas"])


def test_desktop_schema_does_not_repeat_the_shared_contract():
    # A Desktop document that also carried the shared routes would give the
    # frontend a second copy of every shared model and let the two drift. The
    # shared contract keeps its own copy, and its own offline check.
    shared = runpy.run_path(str(SCRIPT))["build_schema"]()
    desktop = json.loads(_run_desktop_script())
    shared_paths = set(shared["paths"])
    assert shared_paths, "An empty shared path scan is not evidence"
    desktop_paths = set(desktop["paths"])
    assert desktop_paths, "An empty desktop path scan is not evidence"
    assert not shared_paths & desktop_paths, sorted(shared_paths & desktop_paths)


def test_desktop_schema_imports_no_platform_service():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys\n"
            f"runpy.run_path({str(DESKTOP_SCRIPT)!r})['build_schema']()\n"
            "leaked = [n for n in sys.modules "
            "if n.startswith(('wenyi_api', 'psycopg', 'redis', 'arq', 'keyring'))]\n"
            "assert not leaked, leaked\n",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
