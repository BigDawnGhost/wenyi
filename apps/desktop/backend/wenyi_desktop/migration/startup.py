"""Move an existing workspace into layer two, or say why it could not be moved.

The move itself is in `wenyi_desktop.migration.layers`. This module is the
startup decision around it, and the only place that decides what the running
application does when the move cannot finish.

Two rules make that decision safe to make at every start:

- **The old workspace is never a fallback for new writes.** If the move cannot be
  completed, the start says so and the application runs on what layer two already
  has; it does not quietly reopen the old catalog. The old catalog stops being
  written the moment this version first runs, and resuming writes to it would
  make the two layouts disagree about the same project.
- **An unfinished move is resumed, never discarded.** The sentinel records that a
  move started, so the next start runs the same idempotent steps over whatever the
  old workspace still holds. Keeping the old data is what makes that possible.

The old workspace is only ever read, and only through
`migration.layers.read`, which uses `open_read_only()`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import paths
from . import layers
from .protocol import VerifyReport

log = logging.getLogger(__name__)


def adopt(workspace: Path, *, home: Path | None = None) -> VerifyReport:
    """Bring layer two up to date for one workspace, before anything opens it.

    Called once per start, ahead of the stores that read the catalog. The
    outcomes are kept distinguishable on purpose, because each leads to a
    different thing the user has to do:

    - layer two is complete, whether this start moved it or an earlier one did;
    - the previous version still owns the workspace, so it has to be closed;
    - the move failed for another reason, and the old data is still intact.
    """
    if not paths.catalog(workspace).is_file():
        # A fresh installation: there is nothing to move, and layer two is
        # created by whoever opens it first.
        return VerifyReport()
    if layers.complete(workspace, home=home):
        return VerifyReport()
    try:
        moved = layers.migrate(workspace, home=home)
    except layers.MigrationBusy:
        raise
    except Exception as error:
        log.warning("Could not move the Desktop data into the user directory", exc_info=True)
        return VerifyReport(problems=(str(error),))
    if moved is None:
        return VerifyReport()
    return VerifyReport(files=len(moved.tables))
