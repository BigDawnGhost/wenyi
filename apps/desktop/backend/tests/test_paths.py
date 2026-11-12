"""The Python and native halves agree on the unified Desktop layout."""

import json
from pathlib import Path

import pytest
from wenyi_desktop import paths
from wenyi_desktop.local_runtime import WorkspaceLock

DESKTOP = Path(__file__).resolve().parents[2]
PROJECT = "0123456789abcdef"


@pytest.mark.parametrize(
    ("resolver", "relative"),
    [
        ("catalog", "catalog.sqlite3"),
        ("projects", "projects"),
        ("runtime_root", "runtime"),
        ("plugins", "runtime/plugins/babeldoc"),
        ("diagnostics", "logs"),
        ("locks", "locks"),
        ("instance_lock", "locks/.desktop.lock"),
        ("native_instance_lock", "locks/.desktop-instance.lock"),
        ("instance_endpoint", "locks/.desktop-instance.json"),
    ],
)
def test_every_path_comes_from_the_explicit_root(tmp_path, resolver, relative):
    root = tmp_path / "data"
    assert getattr(paths, resolver)(root) == root / relative
    assert not root.exists()


def test_runtime_never_searches_the_old_plugin_location(tmp_path, monkeypatch):
    legacy = tmp_path / "plugins" / "babeldoc"
    legacy.mkdir(parents=True)
    (legacy / "plugin.json").write_text("{}")
    monkeypatch.setenv("WENYI_RUNTIME_DIR", str(legacy))
    assert paths.plugins(tmp_path) == tmp_path / "runtime" / "plugins" / "babeldoc"


def test_workspace_lock_matches_the_path_resolver(tmp_path):
    assert WorkspaceLock(tmp_path).path == paths.instance_lock(tmp_path)


def test_project_names_preserve_identity_and_bound_the_suffix():
    assert paths.project_name(PROJECT, "my book") == f"{PROJECT}-my_book"
    assert paths.project_name(PROJECT, "a-b-c") == f"{PROJECT}-a-b-c"
    assert paths.split_project_name(f"{PROJECT}-a-b-c") == (PROJECT, "a-b-c")
    assert len(paths.project_name(PROJECT, "x" * 200)) == 16 + 1 + 64
    for invalid in ("../escape", "0123456789ABCDEF", "a" * 17, ""):
        assert paths.project_id(invalid) == ""


def test_marker_format_is_explicit_and_does_not_adopt_old_versions(tmp_path):
    directory = tmp_path / PROJECT
    directory.mkdir()
    marker = paths.marker_in(directory)
    marker.write_text(json.dumps({"pid": PROJECT, "schema_version": 1}))
    with pytest.raises(ValueError, match="Unsupported"):
        paths.project_id_in(directory)
    marker.write_text(json.dumps({"pid": PROJECT, "schema_version": paths.PROJECT_SCHEMA_VERSION}))
    assert paths.project_id_in(directory) == PROJECT


def test_shared_native_names_match_the_engine():
    native = (DESKTOP / "src/paths.rs").read_text()
    # Catalog and project paths belong exclusively to the Python backend.
    for relative in ("logs", "locks", "runtime", "backups"):
        assert f'"{relative}"' in native
