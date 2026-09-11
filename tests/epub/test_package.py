from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

from tests.fixtures.books import write_phase9_epub, write_sample_epub
from trans_novel.assemble import preflight_epub
from trans_novel.assemble.epub.rendering.source_archive import assemble_epub
from trans_novel.assemble.epub.verification import validate_epub, verify_epub
from trans_novel.epub.slots import distribute_slot_translation
from trans_novel.ingest.epub.reader import read_epub


class TestEpubPackage(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.epub"
        write_sample_epub(str(self.source))
        with zipfile.ZipFile(self.source) as archive:
            self.files = {name: archive.read(name) for name in archive.namelist()}

    def _save(self) -> None:
        with zipfile.ZipFile(self.source, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in self.files.items():
                compression = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
                archive.writestr(name, data, compression)

    def test_declared_html_without_html_suffix_is_translated_and_preserved(self) -> None:
        opf = self.files["OEBPS/content.opf"]
        self.files["OEBPS/chapter.xml"] = self.files.pop("OEBPS/ch1.xhtml")
        auxiliary = self.files.pop("OEBPS/ch2.xhtml").replace(b"<html ", b'<html lang="ja" ')
        self.files["OEBPS/auxiliary.bin"] = auxiliary
        opf = opf.replace(b"ch1.xhtml", b"chapter.xml").replace(b"ch2.xhtml", b"auxiliary.bin")
        opf = opf.replace(b'<itemref idref="ch2"/>', b"")
        for media in (b"application/xhtml+xml", b"text/html"):
            with self.subTest(media=media):
                self.files["OEBPS/content.opf"] = opf.replace(b"application/xhtml+xml", media)
                self._save()
                doc = read_epub(str(self.source), "ja", "zh-Hans")
                self.assertEqual([chapter.href for chapter in doc.chapters], ["OEBPS/chapter.xml"])
                for chapter in doc.chapters:
                    for segment in chapter.segments:
                        segment.assign_translation(
                            distribute_slot_translation(segment.epub_state, "测试译文")
                        )
                manifest = {
                    "fmt": doc.fmt,
                    "meta": doc.meta,
                    "source_lang": doc.source_lang,
                    "target_lang": doc.target_lang,
                    "chapters": [{"index": chapter.index} for chapter in doc.chapters],
                }
                chapters = {chapter.index: chapter for chapter in doc.chapters}
                store = SimpleNamespace(
                    load_manifest=lambda manifest=manifest: manifest,
                    load_chapter=chapters.__getitem__,
                )
                for bilingual in (False, True):
                    output = self.root / "output.epub"
                    assemble_epub(store, str(self.source), str(output), bilingual=bilingual)
                    report = verify_epub(
                        output,
                        source_path=self.source,
                        store=store,
                        mode="bilingual" if bilingual else "monolingual",
                        bilingual=bilingual,
                    )
                    self.assertTrue(report["passed"], report["failures"])
                    with zipfile.ZipFile(output) as archive:
                        self.assertIn("测试译文", archive.read("OEBPS/chapter.xml").decode())
                        rendered = etree.fromstring(archive.read("OEBPS/auxiliary.bin"))
                        self.assertEqual(rendered.get("lang"), "zh-Hans")
                        self.assertEqual(
                            "".join(rendered.itertext()),
                            "".join(etree.fromstring(auxiliary).itertext()),
                        )

    def test_missing_media_uses_suffix_without_erasing_diagnostic(self) -> None:
        self.files["OEBPS/content.opf"] = self.files["OEBPS/content.opf"].replace(
            b' media-type="application/xhtml+xml"', b""
        )
        self._save()
        doc = read_epub(str(self.source), "ja", "zh")
        self.assertEqual(
            [chapter.href for chapter in doc.chapters], ["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml"]
        )
        self.assertIn(
            "manifest_media_missing",
            {item["code"] for item in validate_epub(self.source)["failures"]},
        )
        for bilingual in (False, True):
            report = preflight_epub(doc, str(self.source), bilingual=bilingual)
            self.assertTrue(report["passed"], report["failures"])

    def test_conflicting_spine_media_is_rejected_instead_of_skipping_chapter(self) -> None:
        original = self.files["OEBPS/content.opf"]
        for media in (b"image/png", b"application/not-html", b"application/x-dtbncx+xml"):
            with self.subTest(media=media):
                self.files["OEBPS/content.opf"] = original.replace(b"application/xhtml+xml", media)
                self._save()
                with self.assertRaisesRegex(ValueError, "non_content_item"):
                    read_epub(str(self.source), "ja", "zh")
                self.assertIn(
                    "non_content_item",
                    {item["code"] for item in validate_epub(self.source)["failures"]},
                )

    def test_spine_order_wins_over_manifest_order(self) -> None:
        self.files["OEBPS/content.opf"] = self.files["OEBPS/content.opf"].replace(
            b'<itemref idref="ch1"/>\n    <itemref idref="ch2"/>',
            b'<itemref idref="ch2"/>\n    <itemref idref="ch1"/>',
        )
        self._save()
        doc = read_epub(str(self.source), "ja", "zh")
        self.assertEqual(
            [chapter.href for chapter in doc.chapters], ["OEBPS/ch2.xhtml", "OEBPS/ch1.xhtml"]
        )

    def test_duplicate_and_missing_manifest_resources_remain_visible(self) -> None:
        self.files["OEBPS/content.opf"] = self.files["OEBPS/content.opf"].replace(
            b"</manifest>",
            b'<item id="ch1" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
            b'<item id="missing" href="absent.bin" media-type="application/unknown"/></manifest>',
        )
        self._save()
        with self.assertRaisesRegex(ValueError, "unresolved_idref"):
            read_epub(str(self.source), "ja", "zh")
        codes = {item["code"] for item in validate_epub(self.source)["failures"]}
        self.assertTrue(
            {"manifest_id_duplicate", "missing_manifest_resource", "unresolved_idref"} <= codes
        )

    def test_missing_media_without_html_suffix_is_rejected(self) -> None:
        self.files["OEBPS/chapter.bin"] = self.files.pop("OEBPS/ch1.xhtml")
        self.files["OEBPS/content.opf"] = (
            self.files["OEBPS/content.opf"]
            .replace(b"ch1.xhtml", b"chapter.bin")
            .replace(b' media-type="application/xhtml+xml"', b"")
        )
        self._save()
        with self.assertRaisesRegex(ValueError, "non_content_item"):
            read_epub(str(self.source), "ja", "zh")

    def test_encoded_nav_path_with_ncx_suffix_uses_manifest_identity(self) -> None:
        write_phase9_epub(str(self.source))
        with zipfile.ZipFile(self.source) as archive:
            self.files = {name: archive.read(name) for name in archive.namelist()}
        self.files["OEBPS/contents page.ncx"] = self.files.pop("OEBPS/nav.xhtml")
        self.files["OEBPS/content.opf"] = self.files["OEBPS/content.opf"].replace(
            b"nav.xhtml", b"contents%20page.ncx"
        )
        self._save()
        doc = read_epub(str(self.source), "en", "zh")
        entries = [
            (entry["kind"], entry["resource_href"])
            for entry in doc.meta["toc_entries"]
            if entry["toc_path"] == "OEBPS/contents page.ncx"
        ]
        self.assertEqual(
            entries,
            [
                ("nav", "OEBPS/text/chapter-1.xhtml"),
                ("nav", "OEBPS/text/chapter-2.xhtml"),
            ],
        )
        report = validate_epub(self.source)
        self.assertEqual(report["failures"], [])
        for bilingual in (False, True):
            report = preflight_epub(doc, str(self.source), bilingual=bilingual)
            self.assertTrue(report["passed"], report["failures"])
