"""slugify bounds long titles so state paths stay under the Windows path limit.

Doc-04 task 01-1.3: a long CJK title used to keep its full length as the
state-directory name and crash path creation past MAX_PATH. The slug is now
capped with a deterministic sha256 suffix, and every platform shares the rule
through slugify.
"""

import os

from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.run_store import (
    SLUG_MAX_LENGTH,
    resolve_file_run_dir,
    slugify,
    translation_run_dir,
)


def test_slugify_caps_long_titles_with_deterministic_suffix():
    """A long CJK title yields a capped slug that is stable across calls."""
    title = "まどか" * 30
    slug = slugify(title)
    assert len(slug) == SLUG_MAX_LENGTH
    assert slug == slugify(title)
    assert slug.startswith("まどか")


def test_slugify_suffix_disambiguates_shared_prefixes():
    """Two long titles that truncate to the same stem still get distinct slugs."""
    base = "非常に長いタイトルの小説" * 10
    left = slugify(base + "上巻")
    right = slugify(base + "下巻")
    assert len(left) == len(right) == SLUG_MAX_LENGTH
    assert left != right


def test_slugify_keeps_short_titles_unchanged():
    """Titles at or below the cap map to themselves, so existing state persists."""
    assert slugify("サンプル小説") == "サンプル小説"
    at_cap = "a" * SLUG_MAX_LENGTH
    assert slugify(at_cap) == at_cap


def test_slugify_boundary_at_cap():
    """One character past the cap flips to the truncated form."""
    over = "b" * (SLUG_MAX_LENGTH + 1)
    capped = slugify(over)
    assert len(capped) == SLUG_MAX_LENGTH
    assert capped != over


def test_slugify_is_idempotent_on_its_own_output():
    """Re-slugging a produced slug is a no-op, so derived names stay stable."""
    slug = slugify("まどか" * 30)
    assert slugify(slug) == slug


def test_long_cjk_title_creates_run_dir_and_glossary(tmp_path):
    """End to end: a title far past the cap still creates state and glossary paths."""
    state_dir = str(tmp_path / "state")
    title = "長い日本語タイトルのテスト小説" * 20
    run_dir = resolve_file_run_dir(state_dir, title, "zh", source_lang="ja", actual_sha256="0" * 64)
    assert run_dir == translation_run_dir(state_dir, title, "zh")
    assert len(slugify(title)) <= SLUG_MAX_LENGTH
    assert run_dir == os.path.join(state_dir, slugify(title), "targets", "zh")

    storage = FileStorage(run_dir)
    assert storage.glossary_path == os.path.join(run_dir, "glossary.db")
    store = GlossaryStore(storage.glossary_path)
    try:
        store.upsert_term(GlossaryTerm(source="魔法少女", target="Magical girl"))
    finally:
        store.close()
    terms = GlossaryStore.load_terms_readonly(storage.glossary_path)
    assert [(term.source, term.target) for term in terms] == [("魔法少女", "Magical girl")]
