"""Native admission is a capability, never a boolean environment escape hatch."""

import io
import json

import pytest
from wenyi_desktop import paths
from wenyi_desktop.ownership import NativeOwner, profile_lock


def test_unverified_pipe_or_environment_cannot_claim_native_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("WENYI_NATIVE_OWNER", "1")
    for line in ("{}\n", "not json\n", '{"native_owner": true}\n'):
        with pytest.raises(RuntimeError, match="ownership proof"):
            NativeOwner.read(io.StringIO(line), tmp_path)


def test_profile_writer_leases_allow_different_workspaces_but_block_upgrade(tmp_path):
    from wenyi_core.storage.locks import FileLockHandle, StorageBusyError

    first, second = tmp_path / "first", tmp_path / "second"
    with profile_lock(first, None):
        with profile_lock(second, None):
            exclusive = FileLockHandle(paths.locks(first) / ".desktop-profile.lock")
            with pytest.raises(StorageBusyError):
                exclusive.acquire(blocking=False)
        assert (paths.locks(first) / ".desktop-profile.lock").exists()
    exclusive.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="profile"):
            with profile_lock(first, None):
                pytest.fail("a new engine entered an upgrade")
    finally:
        exclusive.release()


def test_arbitrary_or_stale_owner_cannot_bypass_profile_lock(tmp_path):
    owner = NativeOwner(tmp_path, 1, 0, [], bytes(32))
    with profile_lock(tmp_path, None):
        with pytest.raises(RuntimeError, match="owner"):
            with profile_lock(tmp_path, owner):
                pytest.fail("unverified native proof bypassed profile admission")
    with pytest.raises(RuntimeError, match="ownership proof"):
        NativeOwner.read(io.StringIO(json.dumps({"pid": 0}) + "\n"), tmp_path)


def test_native_engine_retains_its_own_writer_lease(tmp_path, monkeypatch):
    import os

    from wenyi_core.storage.locks import FileLockHandle, StorageBusyError

    owner = NativeOwner(tmp_path, 1, 1, list(range(16)), bytes(32))
    monkeypatch.setattr(NativeOwner, "valid", lambda self, workspace: True)
    with profile_lock(tmp_path, owner):
        name = f"{bytes(owner.instance).hex()}-{os.getpid()}.lock"
        lease = paths.locks(tmp_path) / ".desktop-writers" / name
        assert lease.is_file()
        other = FileLockHandle(lease)
        with pytest.raises(StorageBusyError):
            other.acquire(blocking=False)
    other.acquire(blocking=False)
    other.release()


def test_native_engine_cannot_register_while_upgrade_holds_admission(tmp_path, monkeypatch):
    from wenyi_core.storage.locks import FileLockHandle

    owner = NativeOwner(tmp_path, 1, 1, list(range(16)), bytes(32))
    monkeypatch.setattr(NativeOwner, "valid", lambda self, workspace: True)
    root = paths.locks(tmp_path)
    root.mkdir(parents=True)
    gate = FileLockHandle(root / ".desktop-profile-admission.lock")
    gate.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="profile"):
            with profile_lock(tmp_path, owner):
                pytest.fail("a native child entered while backup admission was frozen")
    finally:
        gate.release()
