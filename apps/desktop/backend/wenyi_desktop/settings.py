"""Transactional Desktop settings with credential reconciliation outside slow vault I/O."""

from contextlib import contextmanager

from fastapi import HTTPException
from wenyi_backend.config_documents import global_document
from wenyi_backend.global_settings import GlobalSettings, validate_settings
from wenyi_backend.model_registry import project_registry_updates
from wenyi_core.config import Config

from .local_backend import LocalBackend
from .local_credentials import CredentialStore


class DesktopSettings:
    def __init__(self, backend: LocalBackend, credentials: CredentialStore):
        self.backend = backend
        self.credentials = credentials

    def defaults(self) -> Config:
        return self.backend.defaults()

    def load(self, *, connection=None) -> GlobalSettings:
        saved = self.backend.load_settings(connection)
        return (
            GlobalSettings(
                Config.from_dict(global_document(Config.from_dict(saved["config"]))),
                "标准翻译",
                saved["revision"],
            )
            if saved
            else GlobalSettings(Config.from_dict(global_document(self.defaults())))
        )

    @contextmanager
    def guard(self, *, exclusive: bool = False):
        with self.backend.transaction() as connection:
            yield connection

    def project_configs(self, connection):
        return self.backend.project_configs(connection)

    def save(
        self, value, default_template, revision, *, model_renames=None, provider_renames=None
    ) -> GlobalSettings:
        config = validate_settings(value, default_template)
        document = global_document(config)
        store = self.credentials
        with self.guard(exclusive=True) as conn:
            current = self.load(connection=conn)
            if revision != current.revision:
                raise HTTPException(409, "Global settings changed; reload before saving again")
            renames = provider_renames or {}
            old_providers = current.config.llm.providers
            new_providers = config.llm.providers
            if len(set(renames.values())) != len(renames) or any(
                old not in old_providers
                or new not in new_providers
                or old in new_providers
                or new in old_providers
                for old, new in renames.items()
            ):
                raise ValueError("Invalid provider rename")
            updates = project_registry_updates(conn, current.config, config, model_renames or {})
            for pid, project_config in updates:
                self.backend.set_project_config(pid, project_config, connection=conn)
            self.backend.save_settings(
                {"config": document, "template": default_template, "revision": revision + 1},
                conn,
            )
            removed = store.reconcile(set(new_providers), renames, conn)
        store.discard_detached(removed)
        return GlobalSettings(config, default_template, revision + 1)
