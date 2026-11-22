"""Missing backend test dependencies must produce guidance, not a raw traceback."""

import importlib.util
import inspect
from pathlib import Path

import pytest

_CONFTEST_MODULE_NAME = "backend_conftest_under_test"


def _load_conftest(tmp_path):
    """Load the real conftest from an isolated copy so guard wiring is exercised."""
    source = Path(__file__).resolve().parent / "conftest.py"
    target = tmp_path / "conftest_under_test.py"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(_CONFTEST_MODULE_NAME, target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard(tmp_path):
    return _load_conftest(tmp_path)


def test_guard_covers_the_suite_transport_dependencies(guard):
    assert {
        "anyio",
        "fastapi",
        "httpx2",
        "starlette",
        "wenyi_backend",
    } <= set(guard.REQUIRED_TEST_MODULES)


def test_guard_runs_before_the_backend_imports(guard):
    source = inspect.getsource(guard)
    assert source.index("require_test_modules()") < source.index(
        "from wenyi_backend.context import"
    )


def test_missing_test_modules_is_empty_when_everything_is_present(guard, monkeypatch):
    monkeypatch.setattr(guard, "find_spec", lambda name: object())

    assert guard.missing_test_modules() == []


def test_missing_test_modules_lists_missing_modules_in_order(guard, monkeypatch):
    monkeypatch.setattr(
        guard,
        "find_spec",
        lambda name: None if name in {"fastapi", "httpx2"} else object(),
    )

    assert guard.missing_test_modules() == ["fastapi", "httpx2"]


def test_missing_test_modules_treats_probe_failure_as_missing(guard, monkeypatch):
    def broken_probe(name):
        raise ValueError("namespace package confusion")

    monkeypatch.setattr(guard, "find_spec", broken_probe)

    assert guard.missing_test_modules() == list(guard.REQUIRED_TEST_MODULES)


def test_require_test_modules_returns_when_present(guard, monkeypatch):
    monkeypatch.setattr(guard, "find_spec", lambda name: object())

    assert guard.require_test_modules() is None


def test_require_test_modules_exits_nonzero_with_install_guidance(guard, monkeypatch, capsys):
    monkeypatch.setattr(
        guard,
        "find_spec",
        lambda name: None if name == "httpx2" else object(),
    )

    with pytest.raises(SystemExit) as exited:
        guard.require_test_modules()

    assert exited.value.code == 1

    captured = capsys.readouterr()
    message = captured.out + captured.err
    assert "httpx2" in message
    assert "uv sync --locked --all-packages --group dev" in message
    assert "rerun pytest" in message
