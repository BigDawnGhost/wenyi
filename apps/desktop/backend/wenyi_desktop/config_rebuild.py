"""Back up and reconstruct catalog settings without changing project state or queued jobs."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from wenyi_backend.config_rebuild import (
    rebuild_default_template,
    rebuild_settings,
    rebuild_strategy,
)
from wenyi_core.config import Config

if TYPE_CHECKING:
    from .local_backend import LocalBackend

logger = logging.getLogger(__name__)


def rebuild_saved_settings(backend: LocalBackend) -> None:
    """Commit each original settings backup and its replacement in one short transaction."""
    initial = backend.load_settings()
    defaults = backend.defaults() if initial is None else Config()
    with backend.transaction() as connection:
        saved = backend.load_settings(connection)
        if saved is not None:
            rebuilt = rebuild_settings(saved.get("config"))
            original_template = saved.get("template", "标准翻译")
            template = rebuild_default_template(original_template)
            removed = list(rebuilt.removed)
            if template != original_template:
                removed.append("template")
            if removed:
                connection.execute(
                    "INSERT INTO configuration_backups(project_id,document,removed) VALUES(NULL,?,?)",
                    (json.dumps(saved, ensure_ascii=False), json.dumps(removed)),
                )
                updated = {
                    **saved,
                    "config": rebuilt.document,
                    "template": template,
                    "revision": saved.get("revision", 0) + 1,
                }
                backend.save_settings(updated, connection)
                logger.warning("Backed up and rebuilt Desktop global settings")
            defaults = Config.from_dict(rebuilt.document)
        project_ids = [row[0] for row in connection.execute("SELECT id FROM projects ORDER BY id")]

    for pid in project_ids:
        with backend.transaction() as connection:
            row = connection.execute("SELECT document FROM projects WHERE id=?", (pid,)).fetchone()
            if row is None:
                continue
            project = json.loads(row[0])
            saved = backend.load_settings(connection)
            current_defaults = Config.from_dict(saved["config"]) if saved is not None else defaults
            original = project.get("config", {})
            rebuilt = rebuild_settings(original, defaults=current_defaults, project=True)
            original_strategy = project.get("strategy")
            strategy = rebuild_strategy(original_strategy)
            if not rebuilt.changed and not strategy.changed:
                continue
            connection.execute(
                "INSERT INTO configuration_backups(project_id,document,removed) VALUES(?,?,?)",
                (
                    pid,
                    json.dumps(
                        {"config": original, "strategy": original_strategy}, ensure_ascii=False
                    ),
                    json.dumps((*rebuilt.removed, *strategy.removed)),
                ),
            )
            project["config"] = rebuilt.document
            if original_strategy is not None or strategy.changed:
                project["strategy"] = strategy.document
            connection.execute(
                "UPDATE projects SET document=? WHERE id=?",
                (json.dumps(project, ensure_ascii=False), pid),
            )
            logger.warning("Backed up and rebuilt Desktop project settings: %s", pid)
