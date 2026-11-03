"""Checkpoint-race integrity for read-only glossary snapshots.

The plain contract -- "a snapshot sees committed WAL data and leaves the formal database
files alone" -- is pinned end-to-end by
`test_file_storage.py::test_reading_terms_keeps_formal_glossary_and_wal_unchanged` and
`test_sqlite_storage.py::test_glossary_rules_and_wal_readonly_snapshot`, which drive the
same `GlossaryStore.load_terms_readonly` path through each storage adapter and compare
bytes plus mtimes. What only this file can pin is the race the adapters never provoke:
a checkpoint landing between the database and WAL copies.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm


class TestReadonlyGlossarySnapshotCheckpointRace(unittest.TestCase):
    def test_retries_when_checkpoint_changes_db_and_wal_between_copies(self):
        """A checkpoint during DB/WAL copying must not produce an accepted mixed-time snapshot."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "glossary.db")
            writer = GlossaryStore(path)
            try:
                writer.upsert_term(
                    GlossaryTerm(source="Ann", target="安", type="person"),
                    chapter=0,
                )
                writer.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.upsert_term(
                    GlossaryTerm(source="Bob", target="鲍勃", type="person"),
                    chapter=0,
                )
                real_copy = shutil.copy2
                checkpointed = False

                def copy_with_checkpoint(source, target, *args, **kwargs):
                    nonlocal checkpointed
                    result = real_copy(source, target, *args, **kwargs)
                    if source == path and not checkpointed:
                        checkpointed = True
                        writer.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    return result

                with patch(
                    "wenyi_core.glossary.store.shutil.copy2",
                    side_effect=copy_with_checkpoint,
                ):
                    terms = GlossaryStore.load_terms_readonly(path)

                self.assertTrue(checkpointed)
                self.assertEqual(
                    [(term.source, term.target) for term in terms],
                    [("Ann", "安"), ("Bob", "鲍勃")],
                )
            finally:
                writer.close()
