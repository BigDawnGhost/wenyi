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
from trans_novel.epub.slots import (
    EpubTextSlot,
    distribute_slot_translation,
    normalized_source_text,
    slot_contract_digest,
)
from trans_novel.ingest.epub.reader import read_epub
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application


class TestEpubFootnotes(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.epub"

    def _book(self, body: bytes, resource: str = "ch1.xhtml") -> None:
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
                info.filename = info.filename.replace("ch1.xhtml", resource)
                archive.writestr(info, data.replace(b"ch1.xhtml", resource.encode()))

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

    def test_incompatible_old_slots_stop_resume_and_export_without_data_loss(self) -> None:
        for old_excludes_marker in (True, False):
            with self.subTest(old_excludes_marker=old_excludes_marker):
                marker = (
                    b'<sup><a href="#n">17</a></sup>'
                    if old_excludes_marker
                    else b'<a role="doc-noteref" href="#n">17</a>'
                )
                self._book(b"<p>Lead " + marker + b' tail</p><p id="n">Note</p>')
                config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
                config.source_lang = "en"
                config.state_dir = str(self.root / str(old_excludes_marker))
                client = FakeClient(handler=routing_handler)
                app = Application(config, client=client)
                store = app.prepare(str(self.source))
                chapter = store.load_chapter(0)
                segment = chapter.segments[0]
                state = segment.epub_state
                if old_excludes_marker:
                    state.slots = [slot for slot in state.slots if slot.source_value != "17"]
                else:
                    state.slots.insert(
                        1,
                        EpubTextSlot(
                            id="legacy-marker", element_path=(0,), field="text", source_value="17"
                        ),
                    )
                state.slot_contract_sha256 = slot_contract_digest(state.slots)
                segment.source = normalized_source_text(state.slots)
                segment.assign_translation(distribute_slot_translation(state, "Saved translation"))
                store.save_chapter(chapter)
                chapter_path = Path(store.chapter_path(0))
                saved = chapter_path.read_bytes()
                client.calls.clear()
                with self.assertRaisesRegex(ValueError, "slot_layout_mismatch"):
                    app.run(str(self.source))
                self.assertEqual(client.calls, [])
                self.assertEqual(chapter_path.read_bytes(), saved)
                output = self.root / "existing.epub"
                output.write_bytes(b"existing output")
                with self.assertRaisesRegex(ValueError, "slot_layout_mismatch"):
                    assemble_source_epub(store, str(self.source), str(output), target_lang="zh")
                self.assertEqual(output.read_bytes(), b"existing output")
                self.assertEqual(chapter_path.read_bytes(), saved)

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
