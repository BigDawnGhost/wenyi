"""One-pass EPUB rendering still owns its chapters; it never owns the snapshot."""

from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest
from wenyi_core.assemble.epub_package import EpubChapter, EpubTocEntry, write_epub
from wenyi_core.assemble.export_view import ExportViewStore
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.epub_reader import read_epub
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.run_store import ExportSnapshotStore


@pytest.fixture
def book(tmp_path):
    source = tmp_path / "original.epub"
    pages = [
        (
            "one.xhtml",
            '<p id="text">The original paragraph.</p><p>A note'
            '<a epub:type="noteref" href="two.xhtml#note">1</a>.</p>',
        ),
        ("two.xhtml", '<aside epub:type="footnote" id="note"><p>Note body.</p></aside>'),
    ]
    write_epub(
        source,
        title="Sample",
        lang="en",
        resources=[],
        chapters=[
            EpubChapter(
                name,
                name,
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                f"{body}</body></html>",
            )
            for name, body in pages
        ],
        toc=[EpubTocEntry(name, name) for name, _ in pages],
    )
    doc = read_epub(str(source), "en", "zh")
    chapter = next(
        c for c in doc.chapters if any(s.source == "The original paragraph." for s in c.segments)
    )
    first = next(s for s in chapter.segments if s.source == "The original paragraph.")
    first.source, first.target = "The original ", '他说"等等.'
    chapter.segments.insert(
        chapter.segments.index(first) + 1,
        Segment(
            index=first.index + 1,
            resource_href=first.resource_href,
            source="paragraph.",
            target='..然后走了"',
            cont=True,
        ),
    )
    for index, segment in enumerate(chapter.segments):
        segment.index = index
    with closing(FileStorage(str(tmp_path / "state"))) as live:
        manifest = live.init_from_document(doc)
        snapshot = live.create_export_snapshot(actual_sha256=manifest["source_sha256"])
        yield source, live, snapshot


def render(store, source, out, **options):
    assemble(store, str(source), str(out), "epub", **options)
    with ZipFile(out) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_epub_projects_exactly_one_deep_copy_per_snapshot_chapter(book, tmp_path, monkeypatch):
    source, _, snapshot = book
    copies = []
    original = Chapter.model_copy

    def count(chapter, **kwargs):
        if kwargs.get("deep"):
            copies.append(chapter.index)
        return original(chapter, **kwargs)

    monkeypatch.setattr(Chapter, "model_copy", count)
    render(snapshot, source, tmp_path / "result.epub")
    assert copies == [row["index"] for row in snapshot.load_manifest()["chapters"]]


@pytest.mark.parametrize("order", ["target_first", "source_first"])
def test_mono_and_bilingual_match_live_export_in_either_sequence(book, tmp_path, order):
    source, live, snapshot = book
    indices = [row["index"] for row in snapshot.load_manifest()["chapters"]]
    before = [live.load_chapter(ci) for ci in indices]
    expected = {
        bi: render(live, source, tmp_path / "reference.epub", bilingual=bi, order=order)
        for bi in (False, True)
    }
    for bi in (False, True, True, False):
        actual = render(snapshot, source, tmp_path / "snapshot.epub", bilingual=bi, order=order)
        assert actual == expected[bi]
        assert '他说"等等...然后走了"' in actual["EPUB/one.xhtml"].decode()
        assert b"two.xhtml#note" in actual["EPUB/one.xhtml"]
    assert [snapshot.load_chapter(ci) for ci in indices] == before
    assert [live.load_chapter(ci) for ci in indices] == before


@pytest.mark.parametrize("target", [None, "", "译文."])
def test_render_copies_preserve_pending_empty_and_translated_targets(tmp_path, target):
    saved = Chapter(
        index=0,
        segments=[
            Segment(index=0, source="First", meta={"nested": [1]}),
            Segment(index=1, source="Second", target=target),
        ],
    )
    snapshot = ExportSnapshotStore(
        str(tmp_path), {"source_lang": "en", "target_lang": "zh"}, {0: saved}
    )
    view = ExportViewStore(snapshot)
    [chapter] = view.iter_chapters_once([0])
    assert chapter == saved
    chapter.segments[0].meta["nested"].append(2)
    assert snapshot.load_chapter(0) == saved
    [again] = view.iter_chapters_once([0])
    assert again.segments[0].meta["nested"] == [1]


def test_fully_translated_chapter_resolves_protected_marker_only_on_its_copy(tmp_path):
    _, segments, _ = annotate_epub_resource(
        '<p>Let <math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math> be positive.</p>',
        0,
        "one.xhtml",
    )
    [segment] = segments
    [item] = segment.meta["epub_annotations"]["items"]
    segment.target = f"设{item['marker']}为正数."
    saved = Chapter(index=0, segments=segments)
    snapshot = ExportSnapshotStore(
        str(tmp_path), {"source_lang": "en", "target_lang": "zh"}, {0: saved}
    )
    view = ExportViewStore(snapshot)
    [rendered] = view.iter_chapters_once([0])
    assert rendered == view.load_chapter(0)
    [result] = rendered.segments
    assert result.target == "设为正数."
    [placement] = result.meta["epub_annotations"]["placements"]
    assert placement["id"] == item["id"] and placement["target_start"] == 1
    assert snapshot.load_chapter(0) == saved


def test_pending_chapter_still_checks_source_and_preserves_destination(book, tmp_path):
    source, live, _ = book
    manifest = live.load_manifest()
    chapter = live.load_chapter(manifest["chapters"][-1]["index"])
    assert all(s.target is None for s in chapter.segments)
    chapter.segments[0].source += "no longer matches"
    live.save_chapter(chapter)
    snapshot = live.create_export_snapshot(actual_sha256=manifest["source_sha256"])
    out = tmp_path / "existing.epub"
    out.write_bytes(b"keep me")
    with pytest.raises(ValueError, match="changed"):
        render(snapshot, source, out)
    assert out.read_bytes() == b"keep me"


def test_one_pass_native_write_failure_preserves_previous_export(book, tmp_path, monkeypatch):
    source, _, snapshot = book
    out = tmp_path / "existing.epub"
    out.write_bytes(b"previous edition")

    def fail(_source, temporary, _state):
        assert Path(temporary) != out
        Path(temporary).write_bytes(b"partial output")
        raise OSError("disk full")

    monkeypatch.setattr("wenyi_core.assemble.epub_native.export", fail)
    with pytest.raises(OSError, match="disk full"):
        render(snapshot, source, out, bilingual=True)
    assert out.read_bytes() == b"previous edition"
