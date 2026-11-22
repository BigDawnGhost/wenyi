"""ExportViewStore shares one resolved chapter per index instead of deep-copying per read."""

from __future__ import annotations

from wenyi_core.assemble.export_view import ExportViewStore
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.storage.run_store import ExportSnapshotStore


def _chapter(index: int) -> Chapter:
    return Chapter(
        index=index,
        href=f"chapter-{index}.xhtml",
        segments=[Segment(index=0, source="Hello.", target="你好。", meta={})],
    )


def _manifest() -> dict:
    return {
        "source_lang": "en",
        "target_lang": "zh",
        "chapters": [
            {"index": 0, "title": "One", "href": "chapter-0.xhtml"},
            {"index": 1, "title": "Two", "href": "chapter-1.xhtml"},
        ],
    }


class FakeStore:
    """Minimal Storage stand-in that returns a fresh chapter copy per load."""

    def __init__(self, run_dir: str, chapters: dict[int, Chapter]) -> None:
        self.run_dir = run_dir
        self.chapters = chapters
        self.loads = 0

    def load_manifest(self) -> dict:
        return _manifest()

    def load_chapter(self, ci: int) -> Chapter:
        self.loads += 1
        return self.chapters[ci].model_copy(deep=True)


def test_load_chapter_hands_out_one_shared_view_copy(tmp_path):
    store = FakeStore(str(tmp_path), {0: _chapter(0)})
    view = ExportViewStore(store)

    first = view.load_chapter(0)
    second = view.load_chapter(0)

    assert first is second
    assert store.loads == 1


def test_load_chapter_keeps_storage_pristine(tmp_path):
    saved = _chapter(0)
    store = FakeStore(str(tmp_path), {0: saved})
    view = ExportViewStore(store)

    handed_out = view.load_chapter(0)
    handed_out.segments[0].target = "caller mutated"

    # The view shares its resolved copy, so later reads through the same view see the
    # caller mutation, while storage and fresh views keep the saved chapter intact.
    assert view.load_chapter(0).segments[0].target == "caller mutated"
    assert ExportViewStore(store).load_chapter(0).segments[0].target == "你好。"
    assert store.load_chapter(0).segments[0].target == "你好。"
    assert saved.segments[0].target == "你好。"


def test_prepare_populates_the_view_once(tmp_path):
    store = FakeStore(str(tmp_path), {0: _chapter(0), 1: _chapter(1)})
    view = ExportViewStore(store)

    view.prepare()
    view.load_chapter(0)
    view.load_chapter(1)

    assert store.loads == 2


def test_iter_chapters_once_snapshot_copies_stay_independent(tmp_path):
    saved = _chapter(0)
    snapshot = ExportSnapshotStore(str(tmp_path / "snapshot"), _manifest(), {0: saved})
    view = ExportViewStore(snapshot)

    first = next(iter(view.iter_chapters_once([0])))
    first.segments[0].meta["touched"] = True

    again = next(iter(view.iter_chapters_once([0])))

    assert "touched" not in again.segments[0].meta
    assert snapshot.load_chapter(0).segments[0].meta == {}
    assert saved.segments[0].meta == {}
