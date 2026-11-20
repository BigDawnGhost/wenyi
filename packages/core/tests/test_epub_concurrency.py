"""EPUB native worker ordering, cross-resource links and atomic error publication."""

from __future__ import annotations

import multiprocessing
import runpy
import sys
import types
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest
from wenyi_core.assemble import epub_native
from wenyi_core.assemble.epub_presentation import _rewrite_html_document
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.epub_reader import read_epub
from wenyi_core.markup.contracts import ANNOTATION_META_KEY
from wenyi_core.storage.file import FileStorage


def _write_epub(path: Path) -> None:
    """Include multi-file chapters, a spine NAV, an auxiliary note and empty image page."""
    names = ["nav.xhtml", *[f"page{i}.xhtml" for i in range(12)], "image.xhtml", "notes.xhtml"]
    manifest = "".join(
        f'<item id="r{i}" href="{name}" media-type="application/xhtml+xml"'
        + (' properties="nav"' if i == 0 else "")
        + "/>"
        for i, name in enumerate(names)
    )
    spine = "".join(f'<itemref idref="r{i}"/>' for i in range(len(names) - 1))
    opf = (
        "<package><metadata><title>Concurrent EPUB</title></metadata>"
        f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
        )
        archive.writestr("content.opf", opf)
        archive.writestr("image.png", b"test-image")
        archive.writestr("style.css", "html { writing-mode: vertical-rl; }")
        archive.writestr(
            "nav.xhtml",
            '<html><body><h1>Contents</h1><nav epub:type="toc"><ol>'
            '<li><a href="page0.xhtml#chapter0">Part one</a></li>'
            '<li><a href="page4.xhtml#chapter4">Part two</a></li>'
            "</ol></nav></body></html>",
        )
        for i in range(12):
            archive.writestr(
                f"page{i}.xhtml",
                "<html><head><title>Resource</title></head><body>"
                f'<h1 id="chapter{i}">Chapter {i}</h1>'
                f'<p id="body{i}">Paragraph {i} <a href="notes.xhtml#note">a phrase</a>.'
                f'<sup id="ref{i}"><a epub:type="noteref" href="notes.xhtml#note">1</a></sup>'
                '<img src="image.png"/></p>'
                "<p>First line<br/>Second line <ruby>字<rt>じ</rt></ruby></p>"
                f'<p><a href="page{(i + 1) % 12}.xhtml#body{(i + 1) % 12}">Next page</a></p>'
                "</body></html>",
            )
        archive.writestr(
            "image.xhtml", '<html><body><img src="image.png" alt="Plate"/></body></html>'
        )
        archive.writestr(
            "notes.xhtml",
            '<html><body><aside epub:type="footnote" id="note"><p>'
            '<a epub:type="backlink" href="page0.xhtml#ref0">1</a> Explanation.'
            "</p><p>More context.</p></aside></body></html>",
        )


def _archive_members(path: Path) -> list[tuple[str, int, bytes]]:
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        return [
            (info.filename, info.compress_type, archive.read(info)) for info in archive.infolist()
        ]


def test_concurrent_native_parse_and_exports_are_deterministic(tmp_path):
    path = tmp_path / "source.epub"
    _write_epub(path)
    original = path.read_bytes()
    serial = read_epub(str(path), "ja", "zh")
    with ThreadPoolExecutor(max_workers=3) as pool:
        copies = list(pool.map(lambda _: read_epub(str(path), "ja", "zh"), range(3)))
    for parallel in copies:
        assert parallel.model_dump() == serial.model_dump()
    assert list(parallel.meta["epub_annotation_contexts"]["contexts"]) == ["notes.xhtml#note"]
    relations = [
        item["relation"]
        for chapter in parallel.chapters
        for segment in chapter.segments
        for item in segment.meta.get(ANNOTATION_META_KEY, {}).get("items", [])
        if item["raw_href"] == "notes.xhtml#note"
    ]
    assert relations and set(relations) == {"noteref"}

    for chapter in serial.chapters:
        for segment in chapter.segments:
            segment.target = "译文：" + segment.source
    paragraph = next(
        segment
        for chapter in serial.chapters
        for segment in chapter.segments
        if segment.kind == "text"
    )
    paragraph.target = ""
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.save_manifest(store.stage_document(serial))
        chapters_dir = Path(store.run_dir) / "chapters"
        before = {p.name: p.read_bytes() for p in chapters_dir.glob("*.json")}
        for bilingual in (False, True):
            for fmt in ("epub", "html"):

                def export(number):
                    # Each export opens an independent connection to the same saved state.
                    with closing(FileStorage(store.run_dir)) as reader:
                        output = tmp_path / f"{fmt}-{bilingual}-{number}.{fmt}"
                        assemble(
                            reader, str(path), str(output), out_format=fmt, bilingual=bilingual
                        )
                        return _archive_members(output) if fmt == "epub" else output.read_bytes()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    left, right = pool.map(export, range(2))
                assert left == right
        assert before == {p.name: p.read_bytes() for p in chapters_dir.glob("*.json")}
    assert path.read_bytes() == original


def test_native_export_failure_preserves_existing_output(tmp_path):
    path = tmp_path / "source.epub"
    _write_epub(path)
    document = read_epub(str(path), "en", "zh")
    document.chapters[0].segments[0].source = "Source state was replaced"
    output = tmp_path / "existing.epub"
    output.write_bytes(b"previous valid export")
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.save_manifest(store.stage_document(document))
        with pytest.raises(ValueError, match="content changed"):
            assemble(store, str(path), str(output))
    assert output.read_bytes() == b"previous valid export"
    assert not list(tmp_path.glob(".*.tmp"))


def test_fused_presentation_is_identical_to_final_writer_rewrite(tmp_path):
    path = tmp_path / "source.epub"
    _write_epub(path)
    document = read_epub(str(path), "ja", "zh")
    for chapter in document.chapters:
        for segment in chapter.segments:
            segment.target = "译文：" + segment.source
    # TOC paths can also be supplied by saved entries; this is not the annotation skip set.
    document.meta["toc_paths"] = []

    def render(**presentation):
        options = epub_native.options(
            document.chapters,
            document.meta,
            book_title=document.title,
            bilingual=True,
            order="source_first",
            preserve_source_style=True,
            source_lang="ja",
            **presentation,
        )
        return epub_native.render_resources(path, options)

    plain = render()
    fused = render(target_lang="zh-Hans", horizontal=True)
    from wenyi_core.assemble.epub_navigation import _is_nav, _rewrite_toc

    for href, html in plain.items():
        data = html.encode()
        if _is_nav(data) or any(
            entry["toc_path"] == href for entry in document.meta["toc_entries"]
        ):
            data = _rewrite_toc(data, document.meta["toc_entries"], is_ncx=False, toc_path=href)
        assert fused[href].encode() == _rewrite_html_document(
            data, lang="zh-Hans", force_horizontal=True, bilingual=False
        )


@pytest.mark.parametrize("entry", ["cli", "desktop"])
def test_frozen_entries_dispatch_workers_before_application_imports(monkeypatch, entry):
    class WorkerDispatched(Exception):
        pass

    def dispatch():
        raise WorkerDispatched

    monkeypatch.setattr(multiprocessing, "freeze_support", dispatch)
    root = Path(__file__).resolve().parents[3]
    with pytest.raises(WorkerDispatched):
        runpy.run_path(str(root / f"scripts/dev/{entry}_entry.py"), run_name="__main__")


@pytest.mark.parametrize("entry", ["cli", "desktop"])
def test_frozen_entries_start_application_once_after_dispatch(monkeypatch, entry):
    order = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: order.append("freeze"))
    module_name = "wenyi_cli.main" if entry == "cli" else "wenyi_desktop.__main__"
    module = types.ModuleType(module_name)
    monkeypatch.setattr(
        module, "main", Mock(side_effect=lambda: order.append("app")), raising=False
    )
    monkeypatch.setitem(sys.modules, module_name, module)
    root = Path(__file__).resolve().parents[3]
    runpy.run_path(str(root / f"scripts/dev/{entry}_entry.py"), run_name="__main__")
    assert order == ["freeze", "app"]
