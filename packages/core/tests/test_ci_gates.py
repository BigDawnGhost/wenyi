"""Exercise CI admission with the same signals consumed by the workflow jobs."""

from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("path", "contract_changed", "expected"),
    [
        ("packages/backend/wenyi_backend/routers/projects.py", False, True),
        ("packages/core/wenyi_core/config.py", True, True),
        ("apps/desktop/backend/wenyi_desktop/main.py", True, True),
        ("apps/web/src/main.tsx", False, True),
        ("pnpm-lock.yaml", False, True),
        ("docs/web.md", False, False),
    ],
)
def test_contract_or_browser_signal_always_builds_before_e2e(path, contract_changed, expected):
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    filters = yaml.safe_load(jobs["changes"]["steps"][1]["with"]["filters"])
    signals = {
        name: str(any(fnmatchcase(path, pattern) for pattern in patterns)).lower()
        for name, patterns in filters.items()
    }
    needs = SimpleNamespace(
        changes=SimpleNamespace(outputs=SimpleNamespace(**signals)),
        contract=SimpleNamespace(outputs=SimpleNamespace(changed=str(contract_changed).lower())),
    )

    def admits(job):
        expression = jobs[job]["if"].replace("&&", "and").replace("||", "or")
        return eval(
            expression.replace("\n", " "),
            {"__builtins__": {}},
            {
                "always": lambda: True,
                "github": SimpleNamespace(event_name="pull_request"),
                "needs": needs,
            },
        )

    builds = admits("web")
    needs.web = SimpleNamespace(result="success" if builds else "skipped")
    assert builds is expected
    assert admits("e2e") is expected


@pytest.mark.parametrize(
    "name",
    [
        "Offline Python regression tests",
        "Service integration tests (PostgreSQL and Redis enabled)",
    ],
)
def test_both_python_versions_run_the_full_workspace(name):
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    assert jobs["python"]["strategy"]["matrix"]["python-version"] == ["3.10", "3.12"]
    step = next(step for step in jobs["python"]["steps"] if step.get("name") == name)
    # The interpreter matrix must not silently narrow the testpaths from pyproject.toml.
    assert step["run"].strip().startswith("uv run --no-sync pytest ")
    assert "packages/core/tests" not in step["run"]


def test_windows_full_tier_uses_bash_for_the_tls_graph_check():
    jobs = yaml.safe_load((ROOT / ".github/workflows/desktop.yml").read_text())["jobs"]
    steps = jobs["local-package"]["steps"]
    check = next(step for step in steps if "links no C TLS" in step.get("name", ""))
    assert check["shell"] == "bash"
    windows = next(
        step for step in steps if step.get("name") == "Test Windows local state and artifact paths"
    )
    assert "packages/core/tests/test_file_migrations.py" in windows["run"]
