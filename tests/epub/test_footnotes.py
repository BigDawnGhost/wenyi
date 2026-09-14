from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

from tests.fixtures.books import write_sample_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble.epub.rendering import assemble_source_epub
from trans_novel.assemble.epub.verification.validation import validate_epub
from trans_novel.config import Config
from trans_novel.epub.slots import distribute_slot_translation
from trans_novel.ingest.epub.reader import read_epub
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application


class TestEpubFootnotes(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.epub"

    def _book(
        self,
        body: bytes,
        resource: str = "ch1.xhtml",
        *,
        note_resource_type: str | None = None,
        nav_note_type: str | None = None,
    ) -> None:
        write_sample_epub(str(self.source))
        with zipfile.ZipFile(self.source) as archive:
            members = [(info, archive.read(info)) for info in archive.infolist()]
        with zipfile.ZipFile(self.source, "w") as archive:
            for info, data in members:
                if info.filename == "OEBPS/ch1.xhtml":
                    data = (
                        b'<html xmlns="http://www.w3.org/1999/xhtml" '
                        b'xmlns:epub="http://www.idpf.org/2007/ops"><head/><body>'
                        + body
                        + b"</body></html>"
                    )
                if info.filename == "OEBPS/content.opf" and note_resource_type is not None:
                    guide = (
                        f'<guide><reference type="{note_resource_type}" '
                        f'href="{resource}"/></guide></package>'
                    ).encode()
                    data = data.replace(b"</package>", guide)
                if info.filename == "OEBPS/content.opf" and nav_note_type is not None:
                    nav_item = (
                        b'<item id="nav" href="nav.xhtml" '
                        b'media-type="application/xhtml+xml" properties="nav"/></manifest>'
                    )
                    data = data.replace(b"</manifest>", nav_item)
                info.filename = info.filename.replace("ch1.xhtml", resource)
                archive.writestr(info, data.replace(b"ch1.xhtml", resource.encode()))
            if nav_note_type is not None:
                archive.writestr(
                    "OEBPS/nav.xhtml",
                    (
                        '<html xmlns="http://www.w3.org/1999/xhtml" '
                        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                        '<nav epub:type="landmarks"><ol><li>'
                        f'<a epub:type="{nav_note_type}" href="{resource}#note">Notes</a>'
                        "</li></ol></nav></body></html>"
                    ),
                )

    def test_explicit_markers_preserve_surrounding_text_and_unmarked_links(self) -> None:
        self._book(
            b'<p>Lead <sup>power <a id="r" epub:type="noteref" href="#n">1</a> units</sup>'
            b' tail <a role="doc-noteref" href="#n">2</a> end '
            b'<sup><a href="#n">3</a></sup> '
            b'<a xmlns:other="urn:other" other:type="noteref" href="#n">foreign</a></p>'
            b'<p id="n">Note <a href="#r">back</a></p>'
        )
        doc = read_epub(str(self.source), "en", "zh")
        segment = doc.chapters[0].segments[0]
        self.assertEqual(segment.source, "Lead power units tail end 3 foreign")
        segment.assign_translation(distribute_slot_translation(segment.epub_state, "Translation"))
        manifest = {
            "meta": doc.meta,
            "source_lang": "en",
            "target_lang": "zh",
            "chapters": [{"index": chapter.index} for chapter in doc.chapters],
        }
        chapters = {chapter.index: chapter for chapter in doc.chapters}
        store = SimpleNamespace(load_manifest=lambda: manifest, load_chapter=chapters.__getitem__)
        output = self.root / "output.epub"
        assemble_source_epub(store, str(self.source), str(output), target_lang="zh")
        with zipfile.ZipFile(output) as archive:
            root = etree.fromstring(archive.read("OEBPS/ch1.xhtml"))
        self.assertEqual(root.xpath('string(//*[@id="r"])'), "1")
        self.assertEqual(root.xpath('string(//*[@role="doc-noteref"])'), "2")
        self.assertEqual(
            read_epub(str(output), "zh", "en").chapters[0].segments[0].source, "Translation"
        )

    def test_ordinary_links_do_not_require_footnote_backlinks(self) -> None:
        self._book(
            b'<p>Lead <a href="footnotes.xhtml#n">1</a> '
            b'<a epub:type="customnoteref" href="#n">2</a> '
            b'<a xmlns:epub="urn:other" epub:type="noteref" href="#n">3</a></p>'
            b'<p id="n">Note</p>',
            resource="footnotes.xhtml",
        )
        self.assertEqual(
            read_epub(str(self.source), "en", "zh").chapters[0].segments[0].source, "Lead 1 2 3"
        )
        report = validate_epub(self.source)
        self.assertEqual(
            [item for item in report["failures"] if item["category"] == "footnotes"], []
        )

    def test_reciprocal_symbol_markers_are_excluded_but_their_tails_remain(self) -> None:
        self._book(
            b'<p>Lead <a id="ref"/><sup><a href="#note"><span>\xe2\x80\xa0</span></a></sup>'
            b' tail</p><p id="note"><a href="#ref">\xe2\x80\xa0</a> Note tail</p>'
        )
        before = self.source.read_bytes()

        document = read_epub(str(self.source), "en", "zh")

        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(document.meta["epub_note_slots_version"], 1)
        self.assertEqual(
            [
                (marker["kind"], marker["label"])
                for marker in document.meta["epub_notes"]["markers"]
            ],
            [("noteref", "†"), ("backlink", "†")],
        )
        self.assertEqual(
            [segment.source for segment in document.chapters[0].segments],
            ["Lead tail", "Note tail"],
        )

    def test_malformed_explicit_markers_are_protected_without_fake_relations(self) -> None:
        self._book(
            b'<p>Lead <a role="doc-noteref" href="#missing"><sup>1</sup></a> tail</p>'
            b'<p><a epub:type="backlink" href="#also-missing">return</a> Note tail</p>'
        )

        document = read_epub(str(self.source), "en", "zh")

        self.assertEqual(
            document.meta["epub_notes"],
            {"version": 1, "markers": [], "targets": []},
        )
        self.assertEqual(
            [segment.source for segment in document.chapters[0].segments],
            ["Lead tail", "Note tail"],
        )

    def test_numeric_pair_uses_exact_package_or_landmark_note_semantics(self) -> None:
        body = (
            b'<p>Lead <a id="ref" href="#note">[12]</a> tail</p>'
            b'<p id="note"><a href="#ref">12</a> Note tail</p>'
        )
        self._book(body)
        ordinary = read_epub(str(self.source), "en", "zh")
        self.assertEqual(
            [segment.source for segment in ordinary.chapters[0].segments],
            ["Lead [12] tail", "12 Note tail"],
        )

        self._book(body, note_resource_type="endnotes")
        notes = read_epub(str(self.source), "en", "zh")

        self.assertEqual(
            [segment.source for segment in notes.chapters[0].segments],
            ["Lead tail", "Note tail"],
        )
        self.assertEqual(notes.meta["epub_notes"]["targets"][0]["kind"], "endnote")

        self._book(body, nav_note_type="notes")
        landmark_notes = read_epub(str(self.source), "en", "zh")
        self.assertEqual(
            [
                segment.source
                for chapter in landmark_notes.chapters
                for segment in chapter.segments
                if segment.resource_href == "OEBPS/ch1.xhtml"
            ],
            ["Lead tail", "Note tail"],
        )
        self.assertEqual(landmark_notes.meta["epub_notes"]["targets"][0]["kind"], "footnote")

    def test_recognized_leaf_aside_note_body_is_extracted(self) -> None:
        self._book(
            b'<p>Lead <a id="ref" href="#note">*</a> tail</p>'
            b'<aside id="note"><a href="#ref">*</a> Aside note tail</aside>'
        )

        document = read_epub(str(self.source), "en", "zh")

        self.assertEqual(
            [segment.source for segment in document.chapters[0].segments],
            ["Lead tail", "Aside note tail"],
        )

    def test_mixed_direct_aside_prose_is_rejected_instead_of_dropped(self) -> None:
        self._book(
            b'<p>Lead <a id="ref" href="#note">*</a> tail</p>'
            b'<aside id="note"><a href="#ref">*</a> Direct prose<p>Nested note</p></aside>'
        )

        with self.assertRaisesRegex(ValueError, "mixed direct prose"):
            read_epub(str(self.source), "en", "zh")

    def test_explicit_reference_semantics_require_a_matching_backlink(self) -> None:
        for attributes in (
            b'role="doc-noteref"',
            b'epub:type="other noteref"',
            b'xmlns:ops="http://www.idpf.org/2007/ops" ops:type="noteref"',
        ):
            with self.subTest(attributes=attributes):
                for backlink in (b"", b'<a href="#r">back</a>'):
                    self._book(
                        b'<p>Lead <a id="r" ' + attributes + b' href="#n">1</a></p>'
                        b'<p id="n">Note ' + backlink + b"</p>"
                    )
                    report = validate_epub(self.source)
                    self.assertEqual(
                        [
                            item["code"]
                            for item in report["failures"]
                            if item["category"] == "footnotes"
                        ],
                        [] if backlink else ["missing_backlink"],
                    )

    def test_compatible_saved_translations_remain_usable(self) -> None:
        self._book(b'<p>Lead <sup><a href="#n">17</a></sup> tail</p><p id="n">Note</p>')
        config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
        config.source_lang = "en"
        config.state_dir = str(self.root / "state")
        app = Application(config, client=FakeClient(handler=routing_handler))
        store = app.prepare(str(self.source))
        chapter = store.load_chapter(0)
        segment = chapter.segments[0]
        segment.assign_translation(
            distribute_slot_translation(segment.epub_state, "Saved translation")
        )
        store.save_chapter(chapter)
        resumed = app.prepare(str(self.source))
        self.assertEqual(resumed.load_chapter(0).segments[0].target, "Saved translation")
        output = self.root / "output.epub"
        assemble_source_epub(resumed, str(self.source), str(output), target_lang="zh")
        with zipfile.ZipFile(output) as archive:
            root = etree.fromstring(archive.read("OEBPS/ch1.xhtml"))
        self.assertIn("Saved translation", "".join(root.itertext()))
