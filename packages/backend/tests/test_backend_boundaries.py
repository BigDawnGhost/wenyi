"""Package imports must enforce deployment boundaries, not merely skip adapters at runtime."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SHARED_FORBIDDEN = {
    "wenyi_api",
    "wenyi_desktop",
    "psycopg",
    "psycopg_pool",
    "redis",
    "arq",
    "keyring",
    "sqlite3",
}


@pytest.mark.parametrize(
    "directory,forbidden",
    [
        (
            "packages/backend/wenyi_backend",
            SHARED_FORBIDDEN,
        ),
        (
            "apps/desktop/backend/wenyi_desktop",
            {"wenyi_api", "psycopg", "psycopg_pool", "redis", "arq"},
        ),
        ("apps/api/wenyi_api", {"wenyi_desktop", "keyring"}),
        ("packages/backend/tests", SHARED_FORBIDDEN),
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


def test_test_support_does_not_import_collected_test_modules():
    """Fixtures and helpers must not make test collection order a dependency."""
    directories = (
        "packages/core/tests",
        "packages/backend/tests",
        "apps/api/tests",
        "apps/desktop/backend/tests",
    )
    violations = []
    for directory in directories:
        files = list((ROOT / directory).rglob("*.py"))
        assert files, f"An empty test-support scan is not evidence: {directory}"
        for file in files:
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules = [entry.name for entry in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                    if not node.module or node.module.split(".")[-1] == "tests":
                        modules.extend(entry.name for entry in node.names)
                else:
                    continue
                for module in modules:
                    if any(part.startswith("test_") for part in module.split(".")):
                        violations.append(f"{file.relative_to(ROOT)}:{node.lineno}: {module}")
    assert not violations, "\n".join(violations)
