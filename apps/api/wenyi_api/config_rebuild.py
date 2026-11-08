"""Transactional settings reconstruction for Web startup, independent of domain state."""

import logging
from pathlib import Path

from psycopg.types.json import Jsonb
from wenyi_backend.config_rebuild import rebuild_settings
from wenyi_core.config import Config, parse_config_yaml

logger = logging.getLogger(__name__)


def rebuild_saved_settings(pool, config_path: str) -> None:
    """Preserve each old JSON document before replacing only its configuration fields."""
    # Mounted bootstrap YAML may be read-only. If it needs repair, keep its backup
    # and repaired settings in PostgreSQL rather than rewriting the mounted file.
    with pool.connection() as connection:
        exists = connection.execute("SELECT 1 FROM application_settings WHERE id=1").fetchone()
    seed = {}
    if exists is None and Path(config_path).is_file():
        raw = parse_config_yaml(Path(config_path).read_text(encoding="utf-8"))
        seed = {} if raw is None else raw
    with pool.connection() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('wenyi:settings',0))")
        row = connection.execute(
            "SELECT document,default_template,revision FROM application_settings WHERE id=1 FOR UPDATE"
        ).fetchone()
        original = row[0] if row else seed
        rebuilt = rebuild_settings(original)
        if rebuilt.changed:
            backup = {
                "config": original,
                "default_template": row[1] if row else "标准翻译",
                "revision": row[2] if row else 0,
            }
            connection.execute(
                "INSERT INTO configuration_backups(project_id,document,removed) VALUES(NULL,%s,%s)",
                (Jsonb(backup), Jsonb(rebuilt.removed)),
            )
            connection.execute(
                """INSERT INTO application_settings(id,document,default_template,revision)
                   VALUES(1,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET
                   document=EXCLUDED.document, revision=EXCLUDED.revision, updated_at=now()""",
                (Jsonb(rebuilt.document), backup["default_template"], backup["revision"] + 1),
            )
            logger.warning("Backed up and rebuilt Web global settings")
        defaults = Config.from_dict(rebuilt.document)
        project_ids = [
            record[0]
            for record in connection.execute("SELECT id FROM projects ORDER BY id").fetchall()
        ]

    for pid in project_ids:
        with pool.connection() as connection:
            # Preserve the same model registry and row through validation and commit.
            connection.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended('wenyi:settings',0))"
            )
            global_row = connection.execute(
                "SELECT document FROM application_settings WHERE id=1"
            ).fetchone()
            current_defaults = Config.from_dict(global_row[0]) if global_row else defaults
            row = connection.execute(
                "SELECT config FROM projects WHERE id=%s FOR UPDATE", (pid,)
            ).fetchone()
            if row is None:
                continue
            rebuilt = rebuild_settings(row[0], defaults=current_defaults, project=True)
            if not rebuilt.changed:
                continue
            connection.execute(
                "INSERT INTO configuration_backups(project_id,document,removed) VALUES(%s,%s,%s)",
                (pid, Jsonb(row[0]), Jsonb(rebuilt.removed)),
            )
            connection.execute(
                "UPDATE projects SET config=%s,updated_at=now() WHERE id=%s",
                (Jsonb(rebuilt.document), pid),
            )
            logger.warning("Backed up and rebuilt Web project settings: %s", pid)
