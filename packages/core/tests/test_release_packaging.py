"""Offline contracts for Git identities and downloadable component assets."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def versions():
    return script("release_version")


@pytest.mark.parametrize(
    ("python", "semver"),
    [
        ("1.2", "1.2.0"),
        ("1.2.3", "1.2.3"),
        ("1.2.3a1", "1.2.3-alpha.1"),
        ("1.2.3b2", "1.2.3-beta.2"),
        ("1.2.3rc4", "1.2.3-rc.4"),
        ("1.2.4.dev3+gabc123", "1.2.4-dev.3+gabc123"),
        ("1.2.3rc2.dev1+gabc", "1.2.3-rc.2.dev.1+gabc"),
        ("1.2.3+d20261001", "1.2.3-dev.0+d20261001"),
        ("255.255.65535", "255.255.65535"),
    ],
)
def test_version_normalization(versions, python, semver):
    assert versions.normalize(python)["version"] == semver


@pytest.mark.parametrize(
    "value", ["1!1.2.3", "1.2.3.post1", "1.2.3.4", "256.1.1", "1.256.1", "1.1.65536"]
)
def test_unsupported_installer_versions_fail(versions, value):
    with pytest.raises(ValueError):
        versions.normalize(value)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Packaging Test")
    git(tmp_path, "config", "user.email", "packaging@example.invalid")
    (tmp_path / "source").write_text("initial")
    git(tmp_path, "add", "source")
    git(tmp_path, "-c", "core.hooksPath=/dev/null", "commit", "-m", "initial")
    git(tmp_path, "tag", "v1.2.3")
    return tmp_path


def test_tag_checkout_and_dirty_contract(versions, repository):
    assert versions.resolve(repository, "v1.2.3")["version"] == "1.2.3"
    # SCM ignores unrelated untracked documentation.
    (repository / "notes").write_text("not product source")
    assert versions.resolve(repository, "v1.2.3")["version"] == "1.2.3"
    (repository / "source").write_text("modified")
    with pytest.raises(ValueError, match="clean"):
        versions.resolve(repository, "v1.2.3")
    assert "-dev." in versions.resolve(repository)["version"]
    git(repository, "add", "source")
    git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-m", "next")
    assert "-dev." in versions.resolve(repository)["version"]
    with pytest.raises(ValueError, match="checkout"):
        versions.resolve(repository, "v1.2.3")
    with pytest.raises(subprocess.CalledProcessError):
        versions.resolve(repository, "v9.9.9")


def test_version_override_rejected(versions, repository, monkeypatch):
    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "9.9.9")
    with pytest.raises(ValueError, match="override"):
        versions.resolve(repository)


def test_explicit_tag_must_match_python_metadata(versions, repository, monkeypatch):
    monkeypatch.setattr(versions, "get_version", lambda **_: "1.2.4")
    with pytest.raises(ValueError, match="disagree"):
        versions.resolve(repository, "v1.2.3")


@pytest.mark.parametrize("tag", ["v1.2.4rc1", "v1.2.4.dev1", "v1.2.4+local"])
def test_release_tag_prerelease_policy(versions, repository, tag):
    git(repository, "tag", tag)
    git(repository, "tag", "-d", "v1.2.3")
    if "rc" in tag:
        assert versions.resolve(repository, tag)["version"] == "1.2.4-rc.1"
    else:
        with pytest.raises(ValueError, match="development/local"):
            versions.resolve(repository, tag)


def test_desktop_bundle_includes_native_icons_for_every_platform():
    project = ROOT / "apps/desktop"
    config = json.loads((project / "tauri.conf.json").read_text())
    icons = {Path(path).suffix: project / path for path in config["bundle"]["icon"]}
    signatures = {".png": b"\x89PNG\r\n\x1a\n", ".ico": b"\x00\x00\x01\x00", ".icns": b"icns"}
    assert signatures.keys() <= icons.keys()
    for suffix, signature in signatures.items():
        assert icons[suffix].read_bytes().startswith(signature)
    # macOS needs a real icon container, including the high-resolution artwork.
    data = icons[".icns"].read_bytes()
    assert int.from_bytes(data[4:8], "big") == len(data)
    cursor = 8
    types = set()
    while cursor < len(data):
        kind = data[cursor : cursor + 4]
        size = int.from_bytes(data[cursor + 4 : cursor + 8], "big")
        assert 8 < size <= len(data) - cursor
        types.add(kind)
        cursor += size
    assert cursor == len(data)
    assert {b"ic09", b"ic10"} <= types


def test_cli_zip_preserves_executable_mode(tmp_path):
    binary = tmp_path / "wenyi"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    target = tmp_path / "cli.zip"
    script("package_release").cli_archive(binary, target)
    with zipfile.ZipFile(target) as archive:
        info = archive.getinfo("wenyi")
        assert info.create_system == 3
        assert stat.S_IMODE(info.external_attr >> 16) == 0o755
        assert archive.read(info) == binary.read_bytes()
    if os.name == "posix":
        # Python's extract intentionally ignores permissions; normal unzip honors them.
        import shutil

        if shutil.which("unzip"):
            subprocess.run(["unzip", str(target), "-d", str(tmp_path / "extracted")], check=True)
            subprocess.run([str(tmp_path / "extracted/wenyi")], check=True)


@pytest.mark.parametrize("version", ["1.2.3", "1.2.4-rc.1", "1.2.4-dev.17+gabc"])
def test_desktop_assets_are_installers_not_bare_binaries(tmp_path, version):
    source = tmp_path / "bundle"
    source.mkdir()
    for directory, name in [
        ("appimage", f"Wenyi Desktop_{version}_amd64.AppImage"),
        ("deb", f"Wenyi Desktop_{version}_amd64.deb"),
        ("rpm", f"Wenyi Desktop-{version}-1.x86_64.rpm"),
    ]:
        (source / directory).mkdir()
        (source / directory / name).write_bytes(b"synthetic package")
    for name in ["wenyi-desktop", "wenyi-desktop.exe", "Wenyi.app.tar.gz"]:
        (source / name).write_bytes(b"synthetic package")
    stem = f"wenyi-desktop-{version}-linux-x64"
    collector = script("package_release")
    outputs = collector.desktop_assets(
        source, tmp_path / "assets", version, "linux-x64", "Wenyi Desktop"
    )
    assert {p.name for p in outputs} == {stem + ext for ext in (".AppImage", ".deb", ".rpm")}
    old = source / "deb/Wenyi Desktop_0.0.1_amd64.deb"
    old.write_bytes(b"old")
    repeated = collector.desktop_assets(
        source, tmp_path / "fresh-assets", version, "linux-x64", "Wenyi Desktop"
    )
    assert all(path.read_bytes() == b"synthetic package" for path in repeated)
    assert old.read_bytes() == b"old"


def test_windows_installers_remain_direct_files(tmp_path):
    source = tmp_path / "bundle"
    for directory, name in [
        ("nsis", "Wenyi Desktop_1.2.3_x64-setup.exe"),
        ("msi", "Wenyi Desktop_1.2.3_x64.msi"),
        ("dmg", "Wenyi Desktop_1.2.3_aarch64.dmg"),
    ]:
        (source / directory).mkdir(parents=True)
        (source / directory / name).write_bytes(b"installer")
    outputs = script("package_release").desktop_assets(
        source, tmp_path / "assets", "1.2.3", "windows-x64", "Wenyi Desktop"
    )
    assert [p.name for p in outputs] == ["wenyi-desktop-1.2.3-windows-x64.exe"]
    assert all(p.read_bytes() == b"installer" for p in outputs)


@pytest.mark.parametrize(
    ("platform", "arch", "other_arch"),
    [("macos-arm64", "aarch64", "x64"), ("macos-x64", "x64", "aarch64")],
)
def test_macos_collects_only_the_requested_dmg(tmp_path, platform, arch, other_arch):
    source = tmp_path / "bundle"
    (source / "dmg").mkdir(parents=True)
    (source / f"dmg/Wenyi Desktop_1.2.3_{arch}.dmg").write_bytes(b"installer")
    (source / f"dmg/Wenyi Desktop_0.0.1_{arch}.dmg").write_bytes(b"old")
    (source / f"dmg/Wenyi Desktop_1.2.3_{other_arch}.dmg").write_bytes(b"other architecture")
    outputs = script("package_release").desktop_assets(
        source, tmp_path / "assets", "1.2.3", platform, "Wenyi Desktop"
    )
    assert [p.name for p in outputs] == [f"wenyi-desktop-1.2.3-{platform}.dmg"]
    assert outputs[0].read_bytes() == b"installer"


@pytest.mark.parametrize(
    ("platform", "other_arch"), [("macos-arm64", "x64"), ("macos-x64", "aarch64")]
)
def test_macos_cannot_collect_other_architecture(tmp_path, platform, other_arch):
    source = tmp_path / "bundle"
    (source / "dmg").mkdir(parents=True)
    (source / f"dmg/Wenyi Desktop_1.2.3_{other_arch}.dmg").write_bytes(b"other architecture")
    with pytest.raises(ValueError, match=f"Missing {platform} 1.2.3"):
        script("package_release").desktop_assets(
            source, tmp_path / "assets", "1.2.3", platform, "Wenyi Desktop"
        )
    assert not (tmp_path / "assets").exists()


def test_collector_does_not_mix_previous_output_with_this_release(tmp_path):
    source = tmp_path / "bundle/nsis"
    source.mkdir(parents=True)
    (source / "Wenyi Desktop_1.2.3_x64-setup.exe").write_bytes(b"new")
    output = tmp_path / "assets"
    output.mkdir()
    previous = output / "wenyi-desktop-0.0.1-windows-x64.exe"
    previous.write_bytes(b"old")
    with pytest.raises(ValueError, match="unrelated assets"):
        script("package_release").desktop_assets(
            source.parent, output, "1.2.3", "windows-x64", "Wenyi Desktop"
        )
    assert list(output.iterdir()) == [previous]
    assert previous.read_bytes() == b"old"


def test_desktop_assets_cannot_relabel_an_old_version(tmp_path):
    source = tmp_path / "bundle"
    (source / "nsis").mkdir(parents=True)
    (source / "nsis/Wenyi Desktop_0.0.1_x64-setup.exe").write_bytes(b"old installer")
    with pytest.raises(ValueError, match="Missing windows-x64 9.9.9"):
        script("package_release").desktop_assets(
            source, tmp_path / "assets", "9.9.9", "windows-x64", "Wenyi Desktop"
        )
    assert not (tmp_path / "assets").exists()


def test_partial_linux_build_is_not_published(tmp_path):
    source = tmp_path / "bundle"
    (source / "appimage").mkdir(parents=True)
    (source / "appimage/Wenyi Desktop_1.2.3_amd64.AppImage").write_bytes(b"installer")
    with pytest.raises(ValueError, match="Missing linux-x64 1.2.3"):
        script("package_release").desktop_assets(
            source, tmp_path / "assets", "1.2.3", "linux-x64", "Wenyi Desktop"
        )
    assert not (tmp_path / "assets").exists()


def test_empty_desktop_bundle_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="Missing windows-x64 1.2.3"):
        script("package_release").desktop_assets(
            tmp_path, tmp_path / "assets", "1.2.3", "windows-x64", "Wenyi Desktop"
        )


def test_sidecar_rebuilds_local_packages_instead_of_reusing_cached_wheels(monkeypatch):
    sidecar = script("desktop_sidecar")
    commands = []
    monkeypatch.setattr(sidecar.sys, "argv", ["desktop_sidecar.py"])
    monkeypatch.setattr(
        sidecar.subprocess, "run", lambda command, **kwargs: commands.append(command)
    )
    sidecar.main()
    sync = commands[0]
    assert sync[:2] == ["uv", "sync"]
    for package in ("wenyi-core", "wenyi-backend", "wenyi-desktop"):
        assert ("--reinstall-package", package) in set(zip(sync, sync[1:]))


@pytest.mark.parametrize("dockerfile", ["Dockerfile.api", "Dockerfile.api.buildx"])
def test_web_images_do_not_reuse_cached_local_package_versions(dockerfile):
    text = (ROOT / "docker" / dockerfile).read_text().replace("\\\n", " ")
    commands = re.findall(r"uv sync [^&;\n]+", text)
    assert len(commands) == 1
    for command in commands:
        assert "--no-cache" in command or all(
            f"--reinstall-package {package}" in command
            for package in ("wenyi-core", "wenyi-backend", "wenyi-api")
        )


def test_workflows_separate_components():
    import yaml

    for component, file in [("cli", "build.yml"), ("desktop", "desktop.yml")]:
        document = yaml.load(
            (ROOT / ".github/workflows" / file).read_text(), Loader=yaml.BaseLoader
        )
        assert component in document["name"].lower()
        assert component in document["run-name"].lower()
        assert "tag" in document["on"]["workflow_dispatch"]["inputs"]
        assert document["on"]["release"]["types"] == ["published"]
        text = str(document)
        assert f"wenyi-{component}-SHA256SUMS.txt" in text
        assert "--clobber" not in text
        assert "tag_name" in text
        assert "tar.gz" not in text
        assert document["jobs"]["release"]["if"] == "github.event_name == 'release'"


@pytest.mark.parametrize("file", ["tests.yml", "build.yml", "desktop.yml"])
def test_workflows_use_main_as_the_only_trunk(file):
    import yaml

    document = yaml.load((ROOT / ".github/workflows" / file).read_text(), Loader=yaml.BaseLoader)
    events = document["on"]
    assert events["pull_request"]["branches"] == ["main"]
    if file == "tests.yml":
        assert events["push"]["branches"] == ["main"]
        assert "paths" not in events["pull_request"]
        assert "paths-ignore" not in events["pull_request"]
    elif file == "desktop.yml":
        # The desktop workflow listens to main only to warm the Rust caches, but
        # the packaging job must never run there: four runners would burn on
        # artefacts nobody consumes.
        assert events["push"]["branches"] == ["main"]
        jobs = document["jobs"]
        assert jobs["local-package"]["if"] == "github.event_name != 'push'"
        assert jobs["cargo-warm"]["if"] == "github.event_name == 'push'"
    else:
        assert "push" not in events


def test_desktop_workflow_builds_native_macos_installers_and_updates():
    import json

    import yaml

    document = yaml.load(
        (ROOT / ".github/workflows/desktop.yml").read_text(), Loader=yaml.BaseLoader
    )
    jobs = document["jobs"]
    # The platform list is produced by the plan job so a pull request can run a
    # reduced subset; the full list is the non-pull-request branch of that step.
    plan_step = next(step for step in jobs["plan"]["steps"] if step.get("id") == "set")
    script = plan_step["run"]
    lines = script.splitlines()
    # The outer if/else picks the event; the inner one only picks the Linux tier.
    outer_else = next(i for i, line in enumerate(lines) if line.strip() == "else")
    full_line = next(line for line in lines[outer_else:] if "matrix='" in line)
    matrix_json = full_line.split("matrix='", 1)[1].rsplit("'", 1)[0]
    matrix = {entry["platform"]: entry for entry in json.loads(matrix_json)}
    assert set(matrix) == {"windows-x64", "linux-x64", "macos-arm64", "macos-x64"}
    for platform, runner in [("macos-arm64", "macos-15"), ("macos-x64", "macos-15-intel")]:
        assert matrix[platform]["os"] == runner
        assert matrix[platform]["bundles"] == "dmg"
        assert matrix[platform]["signed_bundles"] == "dmg,app"

    # A pull request keeps one Linux leg, and its tier follows the changed paths:
    # desktop changes need the full packaging run, business code does not.
    pr_lines = lines[:outer_else]
    pr_block = "\n".join(pr_lines)
    assert "tier=full" in pr_block and "tier=test" in pr_block
    assert "$DESKTOP_CHANGED" in pr_block
    assert '\\"platform\\":\\"linux-x64\\",\\"tier\\":\\"${tier}\\' in pr_block
    assert "windows-x64" in pr_block and "smoke" in pr_block
    # The filter must cover every path the packaging path consumes, including
    # this workflow file so a change to the tiering itself still builds fully.
    filters = next(
        step["with"]["filters"] for step in jobs["plan"]["steps"] if step.get("id") == "filter"
    )
    for expected in (
        "apps/desktop/**",
        "scripts/desktop*",
        "package.json",
        "pnpm-lock.yaml",
        ".github/workflows/desktop.yml",
    ):
        assert expected in filters, expected

    job = jobs["local-package"]
    assert job["needs"] == "plan"
    assert "needs.plan.outputs.matrix" in job["strategy"]["matrix"]
    build = next(
        step["run"]
        for step in job["steps"]
        if step.get("run", "").startswith("pnpm desktop:build ")
    )
    assert (
        "steps.updater.outputs.signed == 'true' && matrix.signed_bundles || matrix.bundles"
    ) in build


def test_cli_workflow_builds_every_platform_on_release_only():
    import json

    import yaml

    document = yaml.load((ROOT / ".github/workflows/build.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = document["jobs"]
    # The platform list comes from the plan job so a pull request builds only one
    # leg; the full list is the else branch, used by release and dispatch.
    script = jobs["plan"]["steps"][0]["run"]
    pr_block = script.split("if", 1)[1].split("else", 1)[0]
    full_block = script.split("else", 1)[1].split("fi", 1)[0]
    pr_matrix = json.loads(pr_block.split("matrix='", 1)[1].rsplit("'", 1)[0])
    full_matrix = json.loads(full_block.split("matrix='", 1)[1].rsplit("'", 1)[0])

    # A pull request keeps exactly one packaging signal on the cheapest runner.
    assert [leg["platform"] for leg in pr_matrix] == ["linux-x64"]
    assert pr_matrix[0]["runner"] == "ubuntu-latest"

    # A release still builds every platform with its original runner and checks.
    by_platform = {leg["platform"]: leg for leg in full_matrix}
    assert set(by_platform) == {
        "windows-x64",
        "linux-x64",
        "linux-arm64",
        "macos-arm64",
        "macos-x64",
    }
    expected = {
        "windows-x64": ("windows-latest", "expected_machine", "8664"),
        "linux-x64": ("ubuntu-latest", "expected_arch", "x86-64"),
        "linux-arm64": ("ubuntu-24.04-arm", "expected_arch", "aarch64"),
        "macos-arm64": ("macos-15", "expected_arch", "arm64"),
        "macos-x64": ("macos-15-intel", "expected_arch", "x86_64"),
    }
    for platform, (runner, key, value) in expected.items():
        assert by_platform[platform]["runner"] == runner
        assert by_platform[platform][key] == value
        assert by_platform[platform]["binary"].startswith("./dist/wenyi")

    # The build job consumes the plan output and keeps failing fast.
    job = jobs["build"]
    assert job["needs"] == "plan"
    assert "needs.plan.outputs.matrix" in job["strategy"]["matrix"]
    assert job["strategy"]["fail-fast"] == "false"
    # Publishing still waits on the build job.
    assert jobs["release"]["needs"] == "build"
