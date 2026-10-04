"""PostgreSQL settings adapter."""

from contextlib import contextmanager, nullcontext

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from wenyi_backend.config_documents import global_document
from wenyi_backend.global_settings import GlobalSettings, validate_settings
from wenyi_backend.model_registry import project_registry_updates
from wenyi_core.config import Config


class PostgresSettings:
    def __init__(self, repository, config_path: str):
        self.repository = repository
        self.config_path = config_path

    def defaults(self):
        return Config.load(self.config_path)

    def load(self, *, connection=None):
        with (
            nullcontext(connection)
            if connection is not None
            else self.repository.pool.connection() as conn
        ):
            row = conn.execute(
                "SELECT document, default_template, revision FROM application_settings WHERE id=1"
            ).fetchone()
        return (
            GlobalSettings(
                Config.from_dict(global_document(Config.from_dict(row[0]))), "标准翻译", row[2]
            )
            if row
            else GlobalSettings(Config.from_dict(global_document(self.defaults())))
        )

    @contextmanager
    def guard(self, *, exclusive=False):
        with self.repository.pool.connection() as conn:
            if exclusive:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('wenyi:settings',0))")
            else:
                conn.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtextextended('wenyi:settings',0))"
                )
            yield conn

    def project_configs(self, connection):
        return connection.execute("SELECT id, config FROM projects").fetchall()

    def save(self, value, default_template, revision, *, model_renames=None, provider_renames=None):
        config = validate_settings(value, default_template)
        document = global_document(config)
        with self.guard(exclusive=True) as conn:
            current = self.load(connection=conn)
            if revision != current.revision:
                raise HTTPException(409, "Global settings changed; reload before saving again")
            updates = project_registry_updates(conn, current.config, config, model_renames or {})
            for pid, project_config in updates:
                conn.execute(
                    "UPDATE projects SET config=%s, updated_at=now() WHERE id=%s",
                    (Jsonb(project_config), pid),
                )
            conn.execute(
                """INSERT INTO application_settings(id, document, default_template, revision)
                   VALUES(1,%s,%s,%s) ON CONFLICT(id) DO UPDATE
                   SET document=EXCLUDED.document, default_template=EXCLUDED.default_template,
                       revision=EXCLUDED.revision, updated_at=now()""",
                (Jsonb(document), default_template, revision + 1),
            )
        return GlobalSettings(config, default_template, revision + 1)
