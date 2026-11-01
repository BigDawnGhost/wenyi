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


def test_execution_services_use_explicit_ports_not_scoped_lookup():
    directory = ROOT / "packages/backend/wenyi_backend/execution"
    files = [file for file in directory.glob("*.py") if file.name != "bindings.py"]
    assert {file.stem for file in files} >= {"runner", "operations", "exporting", "contracts"}
    files += [
        ROOT / "packages/backend/wenyi_backend" / name
        for name in ("project_service.py", "job_service.py", "source_view.py")
    ]
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in {"current_context", "dal", "paths"}, file
            if isinstance(node, ast.ImportFrom):
                assert all(
                    name.name not in {"current_context", "dal", "paths"} for name in node.names
                ), file
    entry = ROOT / "packages/backend/wenyi_backend/workers/tasks.py"
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("wenyi_core.pipeline", "wenyi_core.srt", "wenyi_core.assemble")
            )


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


def test_retired_dynamic_repository_forwarding_has_no_remaining_consumers():
    assert not (ROOT / "packages/backend/wenyi_backend/dal.py").exists()
    files = [
        file
        for directory in ("packages", "apps")
        for file in (ROOT / directory).rglob("*.py")
        if "node_modules" not in file.parts and ".venv" not in file.parts
    ]
    assert files
    retired = "wenyi_backend" + ".dal"
    for file in files:
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.module != retired, file
                if node.module == "wenyi_backend":
                    assert all(name.name != "dal" for name in node.names), file
            elif isinstance(node, ast.Import):
                assert all(name.name != retired for name in node.names), file
    adapter = ROOT / "apps/api/wenyi_api/adapters.py"
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
        for node in ast.walk(ast.parse(adapter.read_text(encoding="utf-8")))
    )
