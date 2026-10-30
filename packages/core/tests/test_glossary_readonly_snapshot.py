"""Read-only glossary snapshot integrity and checkpoint-race tests."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm


class TestReadonlyGlossarySnapshot(unittest.TestCase):
    def test_reads_committed_wal_without_touching_formal_database_files(self):
        """Read-only glossary snapshots must include committed, uncheckpointed WAL data."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "glossary.db")
            writer = GlossaryStore(path)
            try:
                writer.upsert_term(
                    GlossaryTerm(source="Ann", target="安", type="person"),
                    chapter=0,
                )
                watched = [path, f"{path}-wal", f"{path}-shm"]
                before = {
                    item: Path(item).read_bytes() if os.path.exists(item) else None
                    for item in watched
                }

                terms = GlossaryStore.load_terms_readonly(path)

                after = {
                    item: Path(item).read_bytes() if os.path.exists(item) else None
                    for item in watched
                }
                self.assertEqual([(term.source, term.target) for term in terms], [("Ann", "安")])
                self.assertEqual(after, before)
            finally:
                writer.close()

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
