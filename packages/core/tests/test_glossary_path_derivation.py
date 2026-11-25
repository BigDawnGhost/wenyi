"""Glossary database paths flow through the shared slug-based run-dir helpers.

Doc-04 task 4.4: term-store paths reuse the existing slug derivation so later slug
rules (such as length caps) propagate automatically instead of drifting.
"""

import os

from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.run_store import slugify, translation_run_dir


def test_glossary_path_derives_from_slug_helper(tmp_path):
    """Run directory and glossary database both reuse the shared slug derivation."""
    state_dir = str(tmp_path / "state")
    title = "サンプル小説：第二版"
    run_dir = translation_run_dir(state_dir, title, "zh")
    assert run_dir == os.path.join(state_dir, slugify(title), "targets", "zh")

    storage = FileStorage(run_dir)
    assert storage.glossary_path == os.path.join(run_dir, "glossary.db")


def test_readonly_snapshot_reads_term_from_derived_path(tmp_path):
    """A term written through the derived path round-trips through the read-only snapshot."""
    state_dir = str(tmp_path / "state")
    run_dir = translation_run_dir(state_dir, "テスト本", "zh")
    storage = FileStorage(run_dir)
    store = GlossaryStore(storage.glossary_path)
    try:
        store.upsert_term(GlossaryTerm(source="ハルヒ", target="Haruhi"))
    finally:
        store.close()

    terms = GlossaryStore.load_terms_readonly(storage.glossary_path)
    assert [(term.source, term.target) for term in terms] == [("ハルヒ", "Haruhi")]
