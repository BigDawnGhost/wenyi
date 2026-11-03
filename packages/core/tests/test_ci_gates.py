"""Exercise CI admission with the same signals consumed by the workflow jobs."""

import json
import re
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


def _bundle_entries() -> list[tuple[str, str]]:
    """Every (key, value) pair of the bundle members that name paths.

    `resources` maps a source to a destination and `binaries` names a destination
    only, so both spellings of one path can appear in either place. Flattening
    them first is what lets one assertion cover every form the configuration can
    take.
    """
    bundle = json.loads((ROOT / "apps/desktop/tauri.bundle.conf.json").read_text())["bundle"]
    entries: list[tuple[str, str]] = []
    for member in ("resources", "binaries"):
        declared = bundle.get(member)
        if isinstance(declared, dict):
            entries.extend((str(key), str(value)) for key, value in declared.items())
        elif isinstance(declared, list):
            entries.extend((str(item), str(item)) for item in declared)
    return entries


def test_a_bundle_entry_never_names_project_data():
    # design §4.1 P2. Upgrading deletes exactly the files the installer recorded,
    # so a project directory listed here is deleted by the next upgrade. The
    # installer's own record is derived from this list, which makes the list the
    # only place the mistake can be made -- and the only place it can be caught
    # before a user's documents are.
    forbidden = ("workspace", "projects")
    found = [
        spelling
        for entry in _bundle_entries()
        for spelling in entry
        if any(word in spelling.lower() for word in forbidden)
    ]
    assert found == [], (
        "bundle.resources and bundle.binaries must never name project data: " + repr(found)
    )


def test_the_bundle_assertion_reads_a_real_configuration():
    # A guard that silently asserts nothing is worse than no guard. The file is
    # present, and it declares at least one entry for the check above to examine.
    entries = _bundle_entries()
    assert entries, "tauri.bundle.conf.json declares no resources or binaries"
    assert any("sidecar" in value for _, value in entries)


def test_windows_full_tier_uses_bash_for_the_tls_graph_check():
    jobs = yaml.safe_load((ROOT / ".github/workflows/desktop.yml").read_text())["jobs"]
    steps = jobs["local-package"]["steps"]
    check = next(step for step in steps if "links no C TLS" in step.get("name", ""))
    assert check["shell"] == "bash"
    windows = next(
        step for step in steps if step.get("name") == "Test Windows local state and artifact paths"
    )
    assert "packages/core/tests/test_file_migrations.py" in windows["run"]


def _installer_hooks() -> Path:
    """The NSIS hook file the Windows bundle configuration names."""
    config = json.loads((ROOT / "apps/desktop/tauri.windows.conf.json").read_text())
    declared = config["bundle"]["windows"]["nsis"]["installerHooks"]
    return (ROOT / "apps/desktop" / declared).resolve()


def _hook_code() -> str:
    """The hooks file with its comments removed, so prose cannot satisfy a check."""
    return "\n".join(
        line
        for line in _installer_hooks().read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(";")
    )


def test_the_windows_bundle_installs_its_nsis_hooks():
    # tauri-bundler only emits `!include` for the hooks file when this setting
    # exists. Without it the template has no include at all, all four
    # `!ifmacrodef NSIS_HOOK_*` checks are false, and every macro is skipped while
    # the build still succeeds. A hooks file that nothing includes is the failure
    # this asserts against -- not a missing macro body.
    hooks = _installer_hooks()
    assert hooks.is_file(), f"installerHooks names a missing file: {hooks}"
    # The macro name is matched whole. A substring test accepts a renamed macro
    # that still starts with the expected name (NSIS_HOOK_PREINSTALLX), and a
    # renamed macro is a macro the bundler never inserts -- which is the silent
    # skip this asserts against.
    declared = set(re.findall(r"^\s*!macro\s+(\S+)", hooks.read_text(encoding="utf-8"), re.M))
    for macro in ("NSIS_HOOK_PREINSTALL", "NSIS_HOOK_POSTUNINSTALL"):
        assert macro in declared, f"{macro} is missing from {hooks.name}: {sorted(declared)}"


def test_installer_hooks_never_name_bundler_defines():
    # The hooks file is included before the block that defines PRODUCTNAME,
    # BUNDLEID and MANUPRODUCTKEY, so referring to one there expands to nothing or
    # fails to compile -- and either way the guard would name the wrong key. The
    # registry path is spelled out instead; this keeps a later tidy-up from
    # reintroducing the define and silently disabling the check.
    code = _hook_code()
    for define in ("${PRODUCTNAME}", "${BUNDLEID}", "${MANUPRODUCTKEY}"):
        assert define not in code, f"{define} is undefined where the hooks are included"


def test_the_nsis_hook_guards_the_registry_key_rust_also_reads():
    # The key in the hooks file and INSTALL_KEY in src/paths.rs are two spellings
    # of one key: the uninstaller reads the first and the startup check reads the
    # second. A divergence would make the PREINSTALL block never fire.
    rust = (ROOT / "apps/desktop/src/paths.rs").read_text(encoding="utf-8")
    declared = next(
        line.split("=", 1)[1].strip().rstrip(";").strip()
        for line in rust.splitlines()
        if "pub const INSTALL_KEY" in line
    )
    # The Rust literal escapes each separator for the compiler; NSIS does not, so
    # the two spellings are compared after undoing the source escaping.
    assert declared == '"Software\\\\wenyi\\\\Wenyi Desktop"'
    assert declared.strip('"').replace("\\\\", "\\") in _hook_code()


def test_the_nsis_hook_names_layer_two_under_the_user_profile():
    # Layer two is %USERPROFILE%\\Wenyi. The first review of the isolation design
    # put it under %LOCALAPPDATA%, where that path names the disposable runtime
    # layer instead -- and the wrong value leaves the user's real data behind.
    code = _hook_code()
    assert "$PROFILE\\Wenyi" in code
    assert "$LOCALAPPDATA\\Wenyi" not in code


def test_the_nsis_hook_checks_for_a_reparse_point_before_recursing():
    # RMDir /r recurses through a junction, so a project directory a user has
    # replaced with a link to another drive would take that drive's contents with
    # it on the way out. The one recursive removal here is guarded.
    code = _hook_code()
    assert "GetFileAttributes" in code
    assert "${WENYI_REPARSE_POINT}" in code
    remove = code.split("!macro wenyi_remove_tree", 1)[1].split("!macroend", 1)[0]
    assert "RMDir /r" in remove
    assert remove.index("GetFileAttributes") < remove.index("RMDir /r")


def test_the_nsis_hook_rechecks_the_uninstall_checkbox_itself():
    # NSIS_HOOK_POSTUNINSTALL is inserted outside the `delete app data` branch, so
    # that branch does not filter it. Without its own test, an uninstall that never
    # asked to delete anything would delete the user's work.
    code = _hook_code()
    post = code.split("!macro NSIS_HOOK_POSTUNINSTALL", 1)[1]
    assert "$DeleteAppDataCheckboxState" in post
    assert "$UpdateMode" in post
    assert post.index("$DeleteAppDataCheckboxState") < post.index("!insertmacro wenyi_remove_tree")


def _windows_bundle() -> dict:
    """The Windows bundle configuration that carries the NSIS settings."""
    return json.loads((ROOT / "apps/desktop/tauri.windows.conf.json").read_text())


def test_the_installer_mode_is_declared_and_stays_current_user():
    # tauri-bundler defaults to currentUser, so declaring it changes nothing today.
    # It is declared anyway because the hook file's correctness depends on it:
    # installer.nsh names layer two as $PROFILE\\Wenyi, and $PROFILE is the profile
    # of the account running setup. Under perMachine, or when setup is started from
    # an elevated shell, that is the administrator's profile -- the uninstaller
    # would then remove a directory the user never owned and leave the user's real
    # data in place. An inherited default is one edit away from being wrong
    # silently; an explicit value fails the build instead.
    nsis = _windows_bundle()["bundle"]["windows"]["nsis"]
    assert nsis["installMode"] == "currentUser"


def test_the_windows_bundle_refuses_to_install_an_older_version():
    # tauri-bundler defaults allowDowngrades to true, so a setup file carried to
    # a machine running a newer build silently replaces it. The project database
    # records its schema version and refuses to be opened by a build that only
    # knows an older one, so a downgrade would leave a working installation with
    # projects it cannot read. The refusal belongs at the installer, which is the
    # only place that can stop the replacement before it happens.
    windows = _windows_bundle()["bundle"]["windows"]
    assert windows["allowDowngrades"] is False


def test_the_nsis_hook_refuses_the_install_location_before_copying():
    # The four refusals of the isolation design are only refusals if they run
    # before the first file is written. NSIS_HOOK_PREINSTALL is inserted ahead of
    # every File instruction, so the ordering inside that macro is what makes them
    # blocking rather than advisory.
    code = _hook_code()
    pre = code.split("!macro NSIS_HOOK_PREINSTALL", 1)[1].split("!macroend", 1)[0]
    assert pre.index("wenyi_refuse_install_location") < pre.index("Abort")
    # The backup refusal that predates this batch is still there and still the
    # second thing the macro does: this batch added checks in front of it rather
    # than replacing any part of it.
    assert "wenyi_has_backup" in pre
    assert pre.index("wenyi_refuse_install_location") < pre.index("wenyi_has_backup")


def test_the_nsis_hook_compares_paths_by_whole_components():
    # Layer three's default directory is "Wenyi Desktop Data" and the installation
    # is "Wenyi Desktop". A bare prefix comparison calls those two nested and
    # refuses the layout every default install produces, so the separator after the
    # longer name is part of the test rather than an optimisation.
    code = _hook_code()
    contains = code.split("!macro wenyi_contains", 1)[1].split("!macroend", 1)[0]
    assert "StrCmp" in contains or '== "$R0"' in contains
    assert '"\\"' in contains
    # Equality is nesting: an installation that is layer two itself is one
    # uninstall away from taking the settings with it.
    assert contains.count('StrCpy ${RESULT} "1"') == 2


def test_the_nsis_hook_has_no_compiled_helper():
    # The installer and the uninstaller are separate programs and both include
    # this file. A helper expanded into a Function would be defined twice and fail
    # to link, so every helper has to stay a macro of plain instructions.
    code = _hook_code()
    assert "Function " not in code
    assert "Var " not in code
