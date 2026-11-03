"""Move an existing workspace into layer two, or say why it could not be moved.

The move itself is in `wenyi_desktop.migration.layers`. This module is the
startup decision around it, and the only place that decides what the running
application does when the move cannot finish.

Two rules make that decision safe to make at every start:

- **Incomplete migration stops startup.** Neither the old workspace nor a
  partially adopted catalog is opened for application writes after a failure.
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
from .ownership import NativeOwner
from .protocol import VerifyReport

log = logging.getLogger(__name__)


def adopt(
    workspace: Path, *, home: Path | None = None, native_owner: NativeOwner | None = None
) -> VerifyReport:
    """Bring layer two up to date for one workspace, before anything opens it.

    Called once per start, ahead of the stores that read the catalog. The
    outcomes are kept distinguishable on purpose, because each leads to a
    different thing the user has to do:

    - layer two is complete, whether this start moved it or an earlier one did;
    - the previous version still owns the workspace, so it has to be closed;
    - the move failed for another reason, and the old data is still intact.
    """
    try:
        layers.check_pending(workspace, home=home)
        if layers.complete(workspace, home=home):
            return VerifyReport()
        if not paths.catalog(workspace).is_file():
            # Validate existing layer two before allowing even a fresh workspace
            # to open it: store initialization must not conceal catalog damage.
            return VerifyReport()
        moved = layers.migrate(workspace, home=home, native_owner=native_owner)
    except layers.MigrationBusy:
        raise
    except Exception as error:
        log.warning("Could not move the Desktop data into the user directory", exc_info=True)
        raise layers.LayoutError(
            f"Desktop migration could not finish: {error}. "
            "Startup was stopped; keep the original workspace and retry."
        ) from error
    if moved is None:
        return VerifyReport()
    return VerifyReport(files=len(moved.tables))
