"""Package imports must enforce deployment boundaries, not merely skip adapters at runtime."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "directory,forbidden",
    [
        (
            "packages/backend/wenyi_backend",
            {
                "wenyi_api",
                "wenyi_desktop",
                "psycopg",
                "psycopg_pool",
                "redis",
                "arq",
                "keyring",
                "sqlite3",
            },
        ),
        (
            "apps/desktop/backend/wenyi_desktop",
            {"wenyi_api", "psycopg", "psycopg_pool", "redis", "arq"},
        ),
        ("apps/api/wenyi_api", {"wenyi_desktop", "keyring"}),
    ],
)
def test_platform_dependency_direction(directory, forbidden):
    files = list((ROOT / directory).rglob("*.py"))
    assert files, "An empty architecture scan is not evidence of a boundary"
    violations = []
    for file in files:
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            modules = (
                [entry.name for entry in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for module in modules:
                if module.split(".")[0] in forbidden:
                    violations.append(f"{file.relative_to(ROOT)}:{node.lineno}: {module}")
    assert not violations, "\n".join(violations)


def test_shared_code_has_no_platform_selector():
    for file in (ROOT / "packages/backend/wenyi_backend").rglob("*.py"):
        assert "local_backend" not in file.read_text(encoding="utf-8"), file
