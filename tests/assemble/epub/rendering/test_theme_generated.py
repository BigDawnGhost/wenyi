from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

from trans_novel.assemble.epub.rendering.generated import build_epub_from_chapters
from trans_novel.assemble.epub.rendering.theme import ThemeBundle
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.assemble.text import merged_paragraphs
from trans_novel.epub.layout import LayoutAssignment, LayoutProfile, source_node_digest
from trans_novel.epub.package import read_package
from trans_novel.ingest import KIND_HEADING
from trans_novel.ingest.models import Chapter, ChapterProcessing, Segment

_FB2 = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
             xmlns:xlink="http://www.w3.org/1999/xlink">
<description><title-info>
  <book-title>Illustrated Book</book-title>
  <coverpage><image xlink:href="#cover.jpg"/></coverpage>
</title-info></description>
<body><section><title><p>Chapter</p></title>
  <image xlink:href="#inside.png"/><p>Illustrated text.</p>
</section></body>
<binary id="cover.jpg" content-type="image/jpeg">Y292ZXItYnl0ZXM=</binary>
<binary id="inside.png" content-type="image/png">aW5zaWRlLWJ5dGVz</binary>
</FictionBook>
"""


def _theme(chapters: list[Chapter]) -> ThemeService:
    assignments = tuple(
        LayoutAssignment(
            node_id=f"{chapter.index}-{position}",
            resource_href=f"ch{chapter.index}.xhtml",
            path=(position,),
            source_sha256=source_node_digest("h1" if kind == KIND_HEADING else "p", {}, source),
            role=None if kind == KIND_HEADING else "body",
        )
        for chapter in chapters
        for position, (kind, _target, source) in enumerate(merged_paragraphs(chapter))
    )
    profile = LayoutProfile(
        source_sha256="a" * 64,
        inventory_digest="b" * 64,
        policy_version="1",
        assignments=assignments,
        provenance={},
    )
    return ThemeService(
        ThemeBundle(
            general_css=b'[data-tn-role="body"] { color: black; }',
            bilingual_css=b'[data-tn-content="source"] { font-size: .9em; }',
            digest="generated-test",
            policy_version="test",
            provenance=(),
        ),
        layout=profile,
    )


def _processing() -> ChapterProcessing:
    return ChapterProcessing(
        action="preserve",
        review_required=False,
        reason="reference",
        source_sha256="source",
        strategy_version="chapter_semantics_v1",
    )


class _Store:
    def __init__(self, fmt: str, chapters: list[Chapter], *, meta: dict | None = None) -> None:
        self._chapters = {chapter.index: chapter for chapter in chapters}
        self._manifest = {
            "title": "Generated",
            "fmt": fmt,
            "source_lang": "en",
            "target_lang": "zh",
            "meta": meta or {},
            "chapters": [
                {
                    "index": chapter.index,
                    "title": chapter.title,
                    "title_translated": f"译-{chapter.title}",
                }
                for chapter in chapters
            ],
        }

    def load_manifest(self) -> dict:
        return self._manifest

    def load_chapter(self, index: int) -> Chapter:
        return self._chapters[index]


def _chapter_member(archive: zipfile.ZipFile, index: int) -> str:
    return next(name for name in archive.namelist() if name.endswith(f"/ch{index}.xhtml"))


class TestGeneratedThemeRenderer(unittest.TestCase):
    def test_text_and_markdown_keep_exact_mono_and_bilingual_order(self) -> None:
        chapter = Chapter(
            index=0,
            title="Chapter One",
            segments=[
                Segment(index=0, source="Chapter One", target="第一章", kind="heading"),
                Segment(index=1, source="Source <&>", target="目标 <&>"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            for fmt in ("text", "markdown"):
                source = Path(directory) / f"book.{'txt' if fmt == 'text' else 'md'}"
                source.write_text("source", encoding="utf-8")
                for bilingual, order, expected in (
                    (False, "target_first", ["第一章", "目标 <&>"]),
                    (True, "target_first", ["第一章", "目标 <&>", "Source <&>"]),
                    (True, "source_first", ["第一章", "Source <&>", "目标 <&>"]),
                ):
                    with self.subTest(fmt=fmt, bilingual=bilingual, order=order):
                        output = Path(directory) / f"{fmt}-{bilingual}-{order}.epub"
                        plan = build_epub_from_chapters(
                            _Store(fmt, [chapter]),
                            str(source),
                            str(output),
                            bilingual=bilingual,
                            order=order,
                            theme=_theme([chapter]),
                        )
                        self.assertIsNotNone(plan)
                        assert plan is not None
                        self.assertEqual(plan.bilingual, bilingual)
                        with zipfile.ZipFile(output) as archive:
                            chapter_bytes = archive.read(_chapter_member(archive, 0))
                            chapter_root = etree.fromstring(chapter_bytes)
                            children = chapter_root.xpath(
                                "/*[local-name()='html']/*[local-name()='body']/*"
                            )
                        self.assertEqual(["".join(node.itertext()) for node in children], expected)
                        paragraphs = [
                            node for node in children if etree.QName(node).localname == "p"
                        ]
                        self.assertTrue(
                            all(node.get("data-tn-role") == "body" for node in paragraphs)
                        )
                        if bilingual:
                            self.assertEqual(
                                [node.get("data-tn-content") for node in paragraphs],
                                ["target", "source"]
                                if order == "target_first"
                                else ["source", "target"],
                            )

    def test_preserved_chapter_is_unchanged_and_unstyled(self) -> None:
        translated = Chapter(
            index=0,
            title="Story",
            segments=[Segment(index=0, source="Story source", target="故事译文")],
        )
        preserved = Chapter(
            index=1,
            title="References",
            segments=[Segment(index=0, source="Smith, 2020.", target="史密斯，2020。")],
            processing=_processing(),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "book.txt")
            baseline = os.path.join(directory, "baseline.epub")
            output = os.path.join(directory, "preserved.epub")
            store = _Store("text", [translated, preserved])
            build_epub_from_chapters(store, source, baseline, bilingual=False)
            plan = build_epub_from_chapters(
                store,
                source,
                output,
                bilingual=True,
                theme=_theme([translated, preserved]),
            )
            self.assertIsNotNone(plan)
            with (
                zipfile.ZipFile(baseline) as baseline_archive,
                zipfile.ZipFile(output) as archive,
            ):
                baseline_bytes = baseline_archive.read(_chapter_member(baseline_archive, 1))
                preserved_bytes = archive.read(_chapter_member(archive, 1))
            self.assertEqual(preserved_bytes, baseline_bytes)
            root = etree.fromstring(preserved_bytes)
            self.assertEqual("".join(root.itertext()).count("Smith, 2020."), 1)
            self.assertNotIn("史密斯", "".join(root.itertext()))
            self.assertFalse(root.xpath("//*[@data-tn-role or @data-tn-content]"))
            self.assertFalse(root.xpath("//*[local-name()='link' and contains(@href, 'tn-theme')]"))

    def test_fb2_theme_preserves_cover_and_inline_image_bytes(self) -> None:
        chapter = Chapter(
            index=0,
            title="Chapter",
            segments=[
                Segment(index=0, source="Chapter", target="章节", kind="heading"),
                Segment(index=1, source="Illustrated text.", target="插图正文。"),
            ],
            meta={"fb2_images": [{"id": "inside.png", "position": 1}]},
        )
        meta = {"fb2_cover_image": "cover.jpg"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "illustrated.fb2"
            source.write_text(_FB2, encoding="utf-8")
            for bilingual in (False, True):
                with self.subTest(bilingual=bilingual):
                    output = Path(directory) / f"fb2-{bilingual}.epub"
                    plan = build_epub_from_chapters(
                        _Store("fb2", [chapter], meta=meta),
                        str(source),
                        str(output),
                        bilingual=bilingual,
                        order="source_first",
                        theme=_theme([chapter]),
                    )
                    self.assertIsNotNone(plan)
                    with zipfile.ZipFile(output) as archive:
                        names = archive.namelist()
                        cover = next(name for name in names if name.endswith("images/cover.jpg"))
                        inside = next(name for name in names if name.endswith("images/inside.png"))
                        chapter_root = etree.fromstring(archive.read(_chapter_member(archive, 0)))
                        package = read_package(archive, [])
                        cover_page = next(
                            item["path"]
                            for item in package["model"]["resolved"]
                            if item["id"] == "cover"
                        )
                        cover_root = etree.fromstring(archive.read(cover_page))
                        self.assertEqual(archive.read(cover), b"cover-bytes")
                        self.assertEqual(archive.read(inside), b"inside-bytes")
                    image = chapter_root.xpath(
                        "//*[local-name()='img' and @src='images/inside.png']"
                    )
                    self.assertEqual(len(image), 1)
                    self.assertFalse(
                        cover_root.xpath(
                            "//*[@data-tn-role or @data-tn-content or @data-tn-theme-node]"
                        )
                    )
                    self.assertFalse(
                        cover_root.xpath("//*[local-name()='link' and contains(@href, 'tn-theme')]")
                    )
                    texts = [
                        "".join(node.itertext())
                        for node in chapter_root.xpath(
                            "/*[local-name()='html']/*[local-name()='body']/*"
                        )
                        if etree.QName(node).localname != "div"
                    ]
                    self.assertEqual(
                        texts,
                        ["章节", "Illustrated text.", "插图正文。"]
                        if bilingual
                        else ["章节", "插图正文。"],
                    )


if __name__ == "__main__":
    unittest.main()
