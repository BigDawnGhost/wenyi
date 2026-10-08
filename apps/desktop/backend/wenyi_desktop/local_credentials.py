"""Desktop-only credential references, OS vault storage and invocation snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from wenyi_core.config import Config
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.credentials import validate_credential
from wenyi_core.llm.factory import build_client
from wenyi_core.llm.registry import provider_spec
from wenyi_core.llm.router import RoutedLLMClient

SERVICE = "org.wenyi.desktop"


class CredentialUnavailable(RuntimeError):
    """Report safe, actionable vault failures without backend exception contents."""


class CredentialConflict(RuntimeError):
    """A concurrent edit invalidated a prepared credential change."""


@dataclass
class CredentialChange:
    previous: dict
    record: dict
    created: bool = False


def system_keyring():
    """Only use OS vault implementations, never plaintext/chained third-party stores."""
    try:
        import keyring

        backend = keyring.get_keyring()
        candidates = (
            backend.backends
            if type(backend).__module__ == "keyring.backends.chainer"
            else [backend]
        )
        allowed = {
            "keyring.backends.macOS",
            "keyring.backends.Windows",
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
        }
        return next(
            (
                candidate
                for candidate in candidates
                if type(candidate).__module__ in allowed and candidate.priority > 0
            ),
            None,
        )
    except Exception:
        return None


class CredentialStore:
    def __init__(self, workspace: Path, *, vault=None, vault_factory: Callable | None = None):
        self.database = workspace / "local.sqlite3"
        self._vault = vault
        self._vault_factory = vault_factory
        self._lock = RLock()
        self._session: dict[str, str] = {}
        with self._connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS desktop_credentials "
                "(connection TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS desktop_external_credentials "
                "(service TEXT PRIMARY KEY, mode TEXT NOT NULL, reference TEXT, storage TEXT, "
                "revision INTEGER NOT NULL)"
            )

    @property
    def vault(self):
        if self._vault_factory is not None:
            self._vault = self._vault_factory()
            self._vault_factory = None
        return self._vault

    @contextmanager
    def _connection(self, connection=None):
        if connection is not None:
            yield connection
            return
        conn = sqlite3.connect(self.database)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _records(self, connection=None) -> dict:
        with self._connection(connection) as conn:
            return {
                name: json.loads(document)
                for name, document in conn.execute(
                    "SELECT connection, document FROM desktop_credentials"
                )
            }

    def _commit(self, records: dict, connection=None) -> None:
        # Only modes and opaque references reach the local catalog.
        with self._connection(connection) as conn:
            conn.execute("DELETE FROM desktop_credentials")
            conn.executemany(
                "INSERT INTO desktop_credentials VALUES (?, ?)",
                [(name, json.dumps(record)) for name, record in records.items()],
            )

    def _mineru_source(self, connection=None) -> tuple[dict, int]:
        with self._connection(connection) as conn:
            row = conn.execute(
                "SELECT mode, reference, storage, revision FROM desktop_external_credentials "
                "WHERE service = 'mineru'"
            ).fetchone()
        if row is None:
            return {}, 0
        mode, reference, storage, revision = row
        record = {"mode": mode}
        if reference is not None:
            record.update(reference=reference, storage=storage)
        return record, revision

    @staticmethod
    def _validate_mineru_token(secret: str) -> None:
        # MinerU uses a plaintext HTTP bearer token, not an OAuth credential document.
        if not secret or any(not 33 <= ord(char) <= 126 for char in secret):
            raise ValueError("Enter a nonblank, header-safe plaintext MinerU API key")
        if any(char in secret for char in '{}[]"'):
            raise ValueError("Enter a plaintext MinerU API key, not an OAuth document")

    def save_mineru(
        self,
        *,
        secret: str | None = None,
        clear: bool = False,
        storage: Literal["auto", "system", "session"] = "auto",
    ) -> dict:
        """Stage vault I/O, then compare and commit only the service source revision."""
        if clear == (secret is not None) or storage not in {"auto", "system", "session"}:
            raise ValueError("Provide a MinerU API key or Clear")
        if secret is not None:
            self._validate_mineru_token(secret)
        previous, revision = self._mineru_source()
        change = self.prepare(previous, mode="manual", secret=secret, clear=clear, storage=storage)
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if self._mineru_source(conn) != (previous, revision):
                    raise CredentialConflict("MinerU credential changed; reload before saving")
                conn.execute(
                    "INSERT INTO desktop_external_credentials "
                    "(service, mode, reference, storage, revision) VALUES ('mineru', ?, ?, ?, ?) "
                    "ON CONFLICT(service) DO UPDATE SET mode=excluded.mode, "
                    "reference=excluded.reference, storage=excluded.storage, "
                    "revision=excluded.revision",
                    (
                        change.record["mode"],
                        change.record.get("reference"),
                        change.record.get("storage"),
                        revision + 1,
                    ),
                )
        except Exception:
            self.abort(change)
            raise
        self.finish(change)
        return self.mineru_status()

    def mineru_status(self) -> dict:
        """Report locally usable credentials without revealing secrets or vault errors."""
        system_available = self.vault is not None
        record, secret = self._mineru_snapshot()
        available = secret is not None
        if available:
            try:
                self._validate_mineru_token(secret)
            except ValueError:
                available = False
        return {
            "mode": record.get("mode", "manual"),
            "storage": record.get("storage"),
            "available": available,
            "environment": "MINERU_API_KEY",
            "system_storage_available": system_available,
            "requires_key": True,
        }

    def resolve_mineru_token(self) -> str | None:
        """Capture an invocation's env-first token without mutating process environment."""
        _, secret = self._mineru_snapshot()
        if secret is not None:
            self._validate_mineru_token(secret)
        return secret

    def _mineru_snapshot(self) -> tuple[dict, str | None]:
        """Read secrets outside SQL, accepting only a revision-stable source."""
        environment = os.environ.get("MINERU_API_KEY", "").strip()
        if environment:
            return {"mode": "environment"}, environment
        for _ in range(3):
            record, revision = self._mineru_source()
            reference = record.get("reference")
            secret = None
            if reference:
                if record.get("storage") == "session":
                    with self._lock:
                        secret = self._session.get(reference)
                elif self.vault is not None:
                    try:
                        secret = self.vault.get_password(SERVICE, reference)
                    except Exception:
                        secret = None
            if self._mineru_source() == (record, revision):
                return record, secret
        raise CredentialConflict("MinerU credential changed; reload before reading")

    def prepare(
        self,
        previous: dict,
        *,
        mode: Literal["environment", "manual"],
        secret: str | None = None,
        storage: Literal["auto", "system", "session"] = "auto",
        clear: bool = False,
    ) -> CredentialChange:
        """Stage vault I/O without owning a catalog transaction or catalog lock."""
        record = {"mode": mode} if clear else {**previous, "mode": mode}
        change = CredentialChange(previous, record)
        if secret is not None and not clear:
            if not secret.strip():
                raise ValueError("A manual credential cannot be blank; use Clear instead")
            validate_credential(secret)
            reference = uuid4().hex
            change.record = {"mode": mode, "reference": reference, "storage": "system"}
            change.created = True
            if storage != "session":
                try:
                    if self.vault is None:
                        raise CredentialUnavailable("System credential storage is unavailable")
                    self.vault.set_password(SERVICE, reference, secret)
                except Exception:
                    self.abort(change)
                    if storage == "system":
                        raise CredentialUnavailable(
                            "System credential storage failed. Choose session-only storage."
                        ) from None
                else:
                    return change
            change.record["storage"] = "session"
            with self._lock:
                self._session[reference] = secret
        return change

    def commit_change(self, name: str, change: CredentialChange, connection) -> None:
        """Compare and write references only; the caller owns the short SQL transaction."""
        records = self._records(connection)
        if records.get(name, {}) != change.previous:
            raise CredentialConflict("Credential selection changed; reload before saving")
        self._commit({**records, name: change.record}, connection)

    def abort(self, change: CredentialChange) -> None:
        if change.created:
            self._discard(change.record)

    def finish(self, change: CredentialChange) -> None:
        if change.previous.get("reference") != change.record.get("reference"):
            self._discard(change.previous)

    def update(self, connection: str, **options) -> None:
        change = self.prepare(self._records().get(connection, {}), **options)
        try:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.commit_change(connection, change, conn)
        except Exception:
            self.abort(change)
            raise CredentialUnavailable("Could not save credential preferences") from None
        self.finish(change)

    def clear_session(self) -> None:
        """Forget volatile keys without touching persistent OS credentials."""
        with self._lock:
            self._session.clear()

    def _discard(self, record: dict) -> None:
        reference = record.get("reference")
        if not reference:
            return
        with self._lock:
            self._session.pop(reference, None)
        if record.get("storage") == "system" and self.vault is not None:
            try:
                self.vault.delete_password(SERVICE, reference)
            except Exception:
                # Detached references can never be reused by another connection.
                pass

    def _resolve(self, record: dict, provider: ProviderConfig) -> str | None:
        if record.get("mode", "environment") == "environment":
            adapter = provider_spec(provider.kind).adapter_type()
            name = provider.api_key_env or adapter.default_api_key_env
            # Explicit names never fall back to another provider's environment variable.
            return (os.environ.get(name, "").strip() or None) if name else None
        reference = record.get("reference")
        if not reference:
            return None
        if record.get("storage") == "session":
            return self._session.get(reference)
        if self.vault is None:
            return None
        try:
            return self.vault.get_password(SERVICE, reference)
        except Exception:
            return None

    def status(
        self,
        connection: str,
        provider: ProviderConfig,
        *,
        record: dict | None = None,
        available: bool | None = None,
    ) -> dict:
        record = self._records().get(connection, {}) if record is None else record
        adapter = provider_spec(provider.kind).adapter_type()
        mode = record.get("mode", "environment")
        return {
            "mode": mode,
            "storage": record.get("storage") if mode == "manual" else None,
            "available": bool(self._resolve(record, provider)) if available is None else available,
            "environment": provider.api_key_env or adapter.default_api_key_env,
            "system_storage_available": self.vault is not None,
            "requires_key": adapter.requires_api_key,
        }

    def snapshot(self, providers: Mapping[str, ProviderConfig]) -> dict[str, str | None]:
        records = self._records()
        return {
            name: self._resolve(records.get(name, {}), provider)
            for name, provider in providers.items()
        }

    def reconcile(self, connections: set[str], renames: Mapping[str, str], connection=None) -> list:
        """Move opaque references on rename and detach deleted connection credentials."""
        records = {
            renames.get(name, name): value for name, value in self._records(connection).items()
        }
        removed = [record for name, record in records.items() if name not in connections]
        self._commit(
            {name: record for name, record in records.items() if name in connections},
            connection,
        )
        if connection is None:
            self.discard_detached(removed)
        return removed

    def discard_detached(self, records: list) -> None:
        for record in records:
            self._discard(record)


def credential_store() -> CredentialStore:
    from wenyi_backend.context import current_context

    return current_context().settings_store.credentials


def build_local_client(config: Config, store: CredentialStore | None = None) -> RoutedLLMClient:
    """Freeze credentials now, before validation or execution starts."""
    return build_client(
        config, credentials=(store or credential_store()).snapshot(config.llm.providers)
    )
