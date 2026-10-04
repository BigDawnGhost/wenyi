"""Owner-only, atomic storage for coordinated OAuth refresh-token rotation.

Environment credentials select a token family; they are never overwritten on disk. Hashed
aliases let an older shell export locate the family's latest token after a process restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...storage.locks import exclusive_file_lock
from .credentials import OAuthCredential, OAuthCredentialError

_STORE_LOCK = threading.RLock()


def _directory() -> Path:
    override = os.environ.get("WENYI_OAUTH_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(config_home) if config_home else Path.home() / ".config") / "wenyi" / "oauth"


def _alias(provider: str, credential: OAuthCredential) -> str:
    token = credential.refresh_token or credential.access_token
    return hashlib.sha256(f"{provider}\0{token}".encode()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"aliases": {}, "credentials": {}, "pending": []}
    os.chmod(path, 0o600)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(name), dict) for name in ("aliases", "credentials")
    ):
        raise ValueError("invalid credential cache")
    if not isinstance(payload.setdefault("pending", []), list):
        raise ValueError("invalid credential cache")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            os.chmod(temporary, 0o600)
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def coordinated_credential(
    provider: str,
    original: OAuthCredential,
    refresh: Callable[[OAuthCredential], OAuthCredential],
) -> OAuthCredential:
    """Serialize readers/refreshers and persist the replacement before releasing the lock.

    An unknown token is a new login, not permission to reuse another account's cache.
    Cache failures fail closed: retrying an already consumed refresh token is unsafe.
    """
    try:
        directory = _directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        with _STORE_LOCK, exclusive_file_lock(str(directory / "credentials.lock")):
            os.chmod(directory / "credentials.lock", 0o600)
            path = directory / "credentials.json"
            payload = _read(path)
            alias = _alias(provider, original)
            root = payload["aliases"].get(alias, alias)
            if alias in payload["aliases"] and root not in payload["credentials"]:
                raise ValueError("credential cache alias has no saved credential")
            if root in payload["pending"]:
                raise OAuthCredentialError(
                    "A previous OAuth refresh was interrupted or failed before its replacement "
                    "was saved; sign in again to avoid reusing a consumed refresh token."
                )
            saved = payload["credentials"].get(root)
            credential = OAuthCredential(**saved) if saved is not None else original
            if credential.expired():
                # Verify writable atomic storage before consuming a rotating token.
                payload["aliases"][alias] = root
                payload["credentials"][root] = credential.to_payload()
                payload["pending"].append(root)
                _write(path, payload)
                credential = refresh(credential)
                if not credential.access_token:
                    raise OAuthCredentialError("OAuth refresh returned no access token")
                payload["aliases"][_alias(provider, credential)] = root
                payload["credentials"][root] = credential.to_payload()
                payload["pending"].remove(root)
                _write(path, payload)
            return credential
    except (OSError, ValueError, TypeError) as error:
        raise OAuthCredentialError(
            "Cannot read or update the OAuth credential cache; check WENYI_OAUTH_CACHE_DIR "
            "and its permissions. If a refresh already completed, sign in again before retrying."
        ) from error
