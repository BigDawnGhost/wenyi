"""Offline coverage for complete, signed Desktop update release assets."""

from __future__ import annotations

import base64
import importlib.util
import io
import plistlib
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def macos_payload(path, version="1.2.3"):
    content = plistlib.dumps({"CFBundleShortVersionString": version})
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("Wenyi Desktop.app/Contents/Info.plist")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))


@pytest.fixture
def updates():
    spec = importlib.util.spec_from_file_location(
        "desktop_updates", ROOT / "scripts/desktop_updates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def signature():
    # Structurally valid but deliberately non-cryptographic offline fixture.
    packet = base64.b64encode(b"ED" + bytes(72)).decode()
    global_signature = base64.b64encode(bytes(64)).decode()
    document = (
        f"untrusted comment: fixture\n{packet}\n"
        f"trusted comment: fixture\tversion:1.2.3\n{global_signature}"
    )
    return base64.b64encode(document.encode()).decode()


@pytest.fixture(autouse=True)
def configured_public_key(monkeypatch):
    packet = base64.b64encode(b"Ed" + bytes(40)).decode()
    document = f"untrusted comment: fixture\n{packet}\n"
    monkeypatch.setenv("WENYI_UPDATER_PUBLIC_KEY", base64.b64encode(document.encode()).decode())


@pytest.fixture
def assets(tmp_path, updates, signature):
    for platform, (_, _, _, extension) in updates.PLATFORMS.items():
        path = tmp_path / f"wenyi-desktop-1.2.3-{platform}{extension}"
        if platform == "macos-arm64":
            macos_payload(path)
        else:
            path.write_bytes(b"offline payload")
        Path(str(path) + ".sig").write_text(signature)
    return tmp_path


def test_complete_manifest(updates, assets, signature):
    result = updates.manifest(assets, "1.2.3", "v1.2.3", "BigDawnGhost/wenyi")
    assert result["version"] == "1.2.3"
    assert set(result["platforms"]) == {"windows-x86_64", "linux-x86_64", "darwin-aarch64"}
    for platform, (key, _, _, extension) in updates.PLATFORMS.items():
        assert result["platforms"][key] == {
            "signature": signature,
            "url": "https://github.com/BigDawnGhost/wenyi/releases/download/v1.2.3/"
            f"wenyi-desktop-1.2.3-{platform}{extension}",
        }


def test_manifest_keeps_release_notes_as_plain_text(updates, assets):
    notes = "## Changes\n\n<script>Not executable</script>\n中文发行说明\n"
    result = updates.manifest(assets, "1.2.3", "v1.2.3", "BigDawnGhost/wenyi", notes)
    assert result["notes"] == notes


def test_mismatched_key_cannot_be_collected_or_advertised(updates, assets, monkeypatch, tmp_path):
    packet = base64.b64encode(b"Ed" + b"otherkey" + bytes(32)).decode()
    document = f"untrusted comment: fixture\n{packet}\n"
    monkeypatch.setenv("WENYI_UPDATER_PUBLIC_KEY", base64.b64encode(document.encode()).decode())
    with pytest.raises(ValueError, match="key ID differs"):
        updates.manifest(assets, "1.2.3", "v1.2.3", "BigDawnGhost/wenyi")
    source = tmp_path / "bundle"
    (source / "nsis").mkdir(parents=True)
    payload = source / "nsis/Wenyi Desktop_1.2.3_x64-setup.exe"
    payload.write_bytes(b"offline installer")
    Path(str(payload) + ".sig").write_text(
        (assets / "wenyi-desktop-1.2.3-windows-x64.exe.sig").read_text()
    )
    output = tmp_path / "collected"
    with pytest.raises(ValueError, match="key ID differs"):
        updates.collect(source, output, "1.2.3", "windows-x64", "Wenyi Desktop")
    assert not output.exists()


@pytest.mark.parametrize("public_key", [None, "", "not-a-public-key"])
def test_missing_or_malformed_public_key_rejected(updates, assets, monkeypatch, public_key):
    if public_key is None:
        monkeypatch.delenv("WENYI_UPDATER_PUBLIC_KEY")
    else:
        monkeypatch.setenv("WENYI_UPDATER_PUBLIC_KEY", public_key)
    with pytest.raises(ValueError, match="WENYI_UPDATER_PUBLIC_KEY"):
        updates.manifest(assets, "1.2.3", "v1.2.3", "BigDawnGhost/wenyi")


@pytest.mark.parametrize("replacement", ["", "\tversion:1.2.2", "\tversion:1.2.3\tversion:1.2.3"])
def test_manifest_rejects_missing_or_mismatched_signed_version(updates, assets, replacement):
    path = assets / "wenyi-desktop-1.2.3-windows-x64.exe.sig"
    document = base64.b64decode(path.read_text()).decode().replace("\tversion:1.2.3", replacement)
    path.write_text(base64.b64encode(document.encode()).decode())
    with pytest.raises(ValueError, match="signature"):
        updates.manifest(assets, "1.2.3", "v1.2.3", "BigDawnGhost/wenyi")


@pytest.mark.parametrize("version", ["01.2.3", "1.2", "1.2.3-rc.1", "1.2.3+local", "1.2.3-dev.0"])
def test_nonstable_identity_rejected(updates, assets, version):
    with pytest.raises(ValueError, match="stable"):
        updates.manifest(assets, version, f"v{version}", "BigDawnGhost/wenyi")


@pytest.mark.parametrize("kind", ["missing", "empty", "malformed", "payload"])
def test_incomplete_manifest_rejected(updates, assets, kind):
    path = assets / "wenyi-desktop-1.2.3-macos-arm64.app.tar.gz.sig"
    if kind == "missing":
        path.unlink()
    elif kind == "payload":
        path.with_suffix("").unlink()
    else:
        path.write_text("" if kind == "empty" else "invalid")
    with pytest.raises(ValueError):
        updates.manifest(assets, "1.2.3", "v1.2.3", "BigDawnGhost/wenyi")


def test_tag_and_repository_validation(updates, assets):
    with pytest.raises(ValueError, match="tag"):
        updates.manifest(assets, "1.2.3", "v1.2.4", "BigDawnGhost/wenyi")
    with pytest.raises(ValueError, match="repository"):
        updates.manifest(assets, "1.2.3", "v1.2.3", "https://untrusted.example")


def test_short_numeric_tag_retains_packaging_semantics(updates, assets):
    for path in list(assets.iterdir()):
        if path.suffix == ".sig":
            document = base64.b64decode(path.read_text()).decode().replace("1.2.3", "1.2.0")
            path.write_text(base64.b64encode(document.encode()).decode())
        path.rename(path.with_name(path.name.replace("1.2.3", "1.2.0")))
    macos_payload(assets / "wenyi-desktop-1.2.0-macos-arm64.app.tar.gz", "1.2.0")
    result = updates.manifest(assets, "1.2.0", "v1.2", "BigDawnGhost/wenyi")
    assert result["version"] == "1.2.0"
    assert "/download/v1.2/" in result["platforms"]["darwin-aarch64"]["url"]


@pytest.mark.parametrize("platform", ["windows-x64", "linux-x64", "macos-arm64"])
def test_collect_exact_payload_and_no_signature_overwrite(updates, tmp_path, signature, platform):
    source, output = tmp_path / "bundle", tmp_path / "assets"
    _, directory, template, extension = updates.PLATFORMS[platform]
    payload = source / directory / template.format(name="Wenyi Desktop", version="1.2.3")
    payload.parent.mkdir(parents=True)
    if platform == "macos-arm64":
        macos_payload(payload)
    else:
        payload.write_bytes(b"offline installer")
    Path(str(payload) + ".sig").write_text(signature)
    updates.collect(source, output, "1.2.3", platform, "Wenyi Desktop")
    assert (
        output / f"wenyi-desktop-1.2.3-{platform}{extension}"
    ).read_bytes() == payload.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        updates.collect(source, output, "1.2.3", platform, "Wenyi Desktop")


def test_collect_missing_signature_leaves_no_partial_assets(updates, tmp_path):
    source, output = tmp_path / "bundle", tmp_path / "assets"
    (source / "macos").mkdir(parents=True)
    (source / "macos/Wenyi Desktop.app.tar.gz").write_bytes(b"offline payload")
    with pytest.raises(ValueError, match="signature"):
        updates.collect(source, output, "1.2.3", "macos-arm64", "Wenyi Desktop")
    assert not output.exists()


def test_stale_macos_archive_cannot_be_renamed(updates, assets):
    macos_payload(assets / "wenyi-desktop-1.2.3-macos-arm64.app.tar.gz", "1.2.2")
    with pytest.raises(ValueError, match="version differs"):
        updates.manifest(assets, "1.2.3", "v1.2.3", "BigDawnGhost/wenyi")
