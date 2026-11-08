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
        ("docs/guide/web.md", False, False),
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


@pytest.mark.parametrize(
    ("event", "ref", "expected"),
    [
        ("push", "refs/heads/main", True),
        ("workflow_dispatch", "refs/heads/main", True),
        ("pull_request", "refs/pull/42/merge", False),
        ("workflow_dispatch", "refs/heads/docs/local-preview", False),
        ("push", "refs/heads/docs/local-preview", False),
    ],
)
def test_documentation_deploys_only_from_main(event, ref, expected):
    workflow = yaml.safe_load((ROOT / ".github/workflows/docs.yml").read_text())
    jobs = workflow["jobs"]
    upload = next(
        step
        for step in jobs["build"]["steps"]
        if step.get("uses", "").startswith("actions/upload-pages-artifact@")
    )
    assert upload["if"] == jobs["deploy"]["if"]
    expression = jobs["deploy"]["if"].replace("&&", "and")
    assert (
        eval(
            expression,
            {"__builtins__": {}},
            {"github": SimpleNamespace(event_name=event, ref=ref)},
        )
        is expected
    )
    assert jobs["deploy"]["needs"] == "build"


def test_documentation_build_is_read_only_and_uploads_only_the_static_site():
    workflow = yaml.safe_load((ROOT / ".github/workflows/docs.yml").read_text())
    build = workflow["jobs"]["build"]
    deploy = workflow["jobs"]["deploy"]
    assert workflow["permissions"] == {"contents": "read"}
    assert build.get("permissions", workflow["permissions"]) == {"contents": "read"}
    assert deploy["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert deploy["environment"]["name"] == "github-pages"
    assert workflow["concurrency"] == {
        "group": "docs-${{ github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    steps = build["steps"]
    commands = [step["run"] for step in steps if "run" in step]
    assert commands == ["pnpm install --frozen-lockfile", "pnpm docs:build"]
    upload = next(
        step for step in steps if step.get("uses", "").startswith("actions/upload-pages-artifact@")
    )
    assert upload["with"]["path"] == "docs/.vitepress/dist"


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


@pytest.mark.parametrize(
    ("event", "publish", "prerelease", "signs", "uploads"),
    [
        ("release", False, False, True, True),
        ("release", False, True, False, True),
        ("release", True, True, False, True),
        ("workflow_dispatch", False, False, False, False),
        ("workflow_dispatch", True, False, True, True),
        ("pull_request", True, False, False, False),
        ("push", True, False, False, False),
    ],
)
def test_desktop_publication_requires_a_release_or_explicit_rebuild(
    event, publish, prerelease, signs, uploads
):
    workflow = yaml.load(
        (ROOT / ".github/workflows/desktop.yml").read_text(), Loader=yaml.BaseLoader
    )
    option = workflow["on"]["workflow_dispatch"]["inputs"]["publish"]
    assert option["type"] == "boolean" and option["default"] == "false"
    jobs = workflow["jobs"]
    package = jobs["local-package"]
    assert package["needs"] == "plan"
    assert jobs["release"]["needs"] == "local-package"
    signing = next(step for step in package["steps"] if step.get("id") == "updater")
    preflight = next(
        step
        for step in jobs["plan"]["steps"]
        if step.get("name") == "Validate published release rebuild"
    )

    def admits(expression):
        expression = expression.replace("&&", "and").replace("||", "or").replace("!", "not ")
        return eval(
            expression,
            {"__builtins__": {}},
            {
                "github": SimpleNamespace(
                    event_name=event,
                    event=SimpleNamespace(release=SimpleNamespace(prerelease=prerelease)),
                ),
                "inputs": SimpleNamespace(publish=publish),
            },
        )

    assert admits(preflight["if"]) is (event == "workflow_dispatch" and publish)
    assert admits(signing["if"]) is signs
    assert admits(jobs["release"]["if"]) is uploads
    native_tests = next(
        step for step in package["steps"] if step.get("run", "").startswith("cargo nextest run ")
    )
    assert native_tests["env"]["TMPDIR"] == "${{ runner.temp }}"


def test_windows_full_tier_uses_bash_for_the_tls_graph_check():
    jobs = yaml.safe_load((ROOT / ".github/workflows/desktop.yml").read_text())["jobs"]
    steps = jobs["local-package"]["steps"]
    check = next(step for step in steps if "links no C TLS" in step.get("name", ""))
    assert check["shell"] == "bash"
    windows = next(
        step for step in steps if step.get("name") == "Test Windows local state and artifact paths"
    )
    assert "packages/core/tests/test_file_migrations.py" in windows["run"]


def _windows_bundle() -> dict:
    """The Windows bundle configuration that carries the NSIS settings."""
    return json.loads((ROOT / "apps/desktop/tauri.windows.conf.json").read_text())


def _installer_code(member: str) -> str:
    """Configured installer sources, excluding comments."""
    config = json.loads((ROOT / "apps/desktop/tauri.windows.conf.json").read_text())
    path = ROOT / "apps/desktop" / config["bundle"]["windows"]["nsis"][member]
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(";")
    )


def test_windows_installer_uses_explicit_template_and_bilingual_current_user_policy():
    nsis = _windows_bundle()["bundle"]["windows"]["nsis"]
    assert nsis["installMode"] == "currentUser"
    assert nsis["languages"] == ["English", "SimpChinese"]
    assert nsis["displayLanguageSelector"] is True
    template = _installer_code("template")
    assert '!include "{{installer_hooks}}"' in template
    assert "RequestExecutionLevel user" in template
    assert "WenyiRequireUser" in template.split("Function .onInit")[1].split("FunctionEnd")[0]
    assert "IsUserAnAdmin" in _installer_code("installerHooks")


def test_installer_separates_program_and_data_without_restoring_old_default():
    code = _installer_code("template")
    assert r"$LOCALAPPDATA\Programs\${PRODUCTNAME}" in code
    assert r'!define DATA_ROOT "$LOCALAPPDATA\Wenyi\Desktop"' in code
    init = code.split("Function .onInit")[1].split("FunctionEnd")[0]
    assert "$PreviousLayout == 2" in init
    assert "$PreviousLayout != 2" in init
    assert "ReadRegDWORD $PreviousLayout" in init


def test_installer_preflight_runs_before_dependency_or_payload_writes():
    code = _installer_code("template")
    assert code.index("Section Preflight") < code.index("Section WebView2")
    assert code.index("Section WebView2") < code.index("Section ReplacePrevious")
    assert code.index("Section ReplacePrevious") < code.index("Section Install")
    pre = code.split("Section Preflight")[1].split("SectionEnd")[0]
    assert "Call CheckDirectory" in pre and "WenyiRequireStopped" in pre
    assert "ExecWait" not in pre
    replace = code.split("Section ReplacePrevious")[1].split("SectionEnd")[0]
    assert replace.index("WenyiRequireStopped") < replace.index("ExecWait")
    assert 'RMDir "$PreviousDirectory"' in replace
    assert "RMDir /r" not in replace
    assert "/S /UPDATE" in replace
    assert "CloseHandle" in replace and "Call AcquireSetupMutex" in replace


def test_installer_retains_registered_directory_ownership_across_uninstall():
    template = _installer_code("template")
    init = template.split("Function .onInit")[1].split("FunctionEnd")[0]
    assert init.index("Call AcquireSetupMutex") < init.index("ReadRegStr $PreviousDirectory")
    assert "ReadRegDWORD $PreviousLayout" in init
    assert re.search(r'\$\{If\} \$0 == ""\s+StrCpy \$PreviousDirectory ""', init)

    directory = _installer_code("installerHooks").split("!macro WenyiCheckDirectory")[1]
    directory = directory.split("!macroend")[0]
    # The previous uninstaller removes these registry entries. Re-reading them
    # rejects its preserved extra files only after the working app is gone.
    assert "ReadReg" not in directory
    assert "${If} $PreviousDirectory != $INSTDIR" in directory
    assert "${OrIf} $PreviousLayout != 2" in directory
    assert directory.index("WenyiNoLinkedParents") < directory.index("$PreviousDirectory")
    assert "WenyiDisjoint" in directory and "GetTempFileName" in directory
    for stage in ("Preflight", "Install"):
        section = template.split(f"Section {stage}")[1].split("SectionEnd")[0]
        assert "Call CheckDirectory" in section


def test_windows_installer_refuses_downgrades_in_all_modes():
    windows = _windows_bundle()["bundle"]["windows"]
    assert windows["allowDowngrades"] is False
    pre = _installer_code("template").split("Section Preflight")[1].split("SectionEnd")[0]
    assert "SemverCompare" in pre and "$0 == -1" in pre
    assert "${Silent}" not in pre


def test_installer_serializes_setup_and_checks_the_real_engine_without_forcing_shutdown():
    code = _installer_code("installerHooks")
    assert "CreateMutexW" in code and "$1 == 183" in code
    assert 'WenyiMutexFunction "un."' in code
    stopped = code.split("!macro WenyiRequireStopped")[1].split("!macroend")[0]
    assert r"\${MAINBINARYNAME}.exe" in stopped
    assert r"\sidecar\wenyi-engine\wenyi-engine.exe" in stopped
    assert "RmGetList" in stopped
    assert "RmShutdown" not in stopped
    assert "$0 != 0" in stopped
    assert r"$TEMP\MicrosoftEdge" not in _installer_code("template")


def test_uninstaller_deletion_is_opt_in_scoped_and_does_not_follow_nested_links():
    template = _installer_code("template")
    uninstall = template.split("Section Uninstall")[1].split("SectionEnd")[0]
    assert uninstall.index("$UpdateMode != 1") < uninstall.index("$DeleteSettings == 1")
    assert uninstall.index("$DeleteProjects == 1") < uninstall.index(r"${DATA_ROOT}\projects")
    assert "WENYI_LIBRARY_ROOTS" not in uninstall
    assert r"$PROFILE\Wenyi" not in uninstall
    assert r"$LOCALAPPDATA\Wenyi Desktop" not in uninstall
    code = _installer_code("installerHooks")
    remove = code.split("Function un.RemoveTree")[1].split("FunctionEnd")[0]
    assert remove.index("GetFileAttributesW") < remove.index("FindFirst")
    assert "${WENYI_REPARSE_POINT}" in remove
    assert "Call un.RemoveTree" in remove
    assert "RMDir /r" not in remove


def test_installer_compares_whole_components_and_refuses_linked_ancestors():
    code = _installer_code("installerHooks")
    contains = code.split("!macro WenyiContains")[1].split("!macroend")[0]
    assert '"\\"' in contains
    assert '$4 == ""' in contains
    links = code.split("!macro WenyiNoLinkedParents")[1].split("!macroend")[0]
    assert "GetFileAttributesW" in links and "${GetParent}" in links
    directory = code.split("!macro WenyiCheckDirectory")[1].split("!macroend")[0]
    assert directory.index("WenyiNoLinkedParents") < directory.index("GetTempFileName")
    assert "WenyiDisjoint" in directory


def test_installer_normalizes_missing_paths_without_nsis_existing_file_requirement():
    code = _installer_code("installerHooks")
    assert not re.search(r"^\s*GetFullPathName ", code, re.M)
    assert "kernel32::GetFullPathNameW" in code
    assert "kernel32::GetLongPathNameW" in code
    normalize = code.split("!macro WenyiFullPath")[1].split("!macroend")[0]
    assert "${GetParent}" in normalize
    assert "${GetFileName}" in normalize
    assert "${NSIS_MAX_STRLEN}" in normalize


def test_installer_validates_explicit_directory_even_if_nsis_discards_it():
    init = _installer_code("template").split("Function .onInit")[1].split("FunctionEnd")[0]
    raw = init.index("kernel32::GetCommandLineW")
    explicit = init.index('${GetOptions} $0 "/D=" $0')
    assert raw < explicit
    assert "StrCpy $INSTDIR $0" in init[explicit:]
    assert explicit < init.index('$INSTDIR == "${PLACEHOLDER}"')


def test_installer_template_does_not_escape_handlebars_path_variables():
    template = _installer_code("template")
    # Handlebars treats \{{name}} as a literal, not a separator plus expansion.
    # An even number of backslashes emits the separator and expands the field.
    assert all(len(slashes) % 2 == 0 for slashes in re.findall(r"(\\+)\{\{", template))


def test_installer_custom_messages_are_available_in_both_languages():
    code = _installer_code("installerHooks")
    messages = re.findall(r"^LangString (\w+) (\S+) ", code, re.M)
    english = {name for name, language in messages if language == "${LANG_ENGLISH}"}
    chinese = {name for name, language in messages if language == "${LANG_SIMPCHINESE}"}
    assert english and english == chinese
