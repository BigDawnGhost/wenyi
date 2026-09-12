from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from lxml import etree

from tests.fixtures.books import write_phase9_epub
from trans_novel.assemble.epub.rendering import assemble_source_epub
from trans_novel.epub.slots import (
    normalize_slot_transport,
    validate_slot_transport,
)
from trans_novel.ingest.epub.reader import read_epub
from trans_novel.ingest.models import Chapter, ChapterProcessing
from trans_novel.pipeline.state import RunIdentity, RunState, RunStore
from trans_novel.postprocess.punct import normalize_zh, normalize_zh_parts

_CONTAINER = b"""<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="O/content.opf"/></rootfiles></container>"""
_OPF = b"""<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title><dc:language>en</dc:language></metadata><manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>"""


class _Store:
    def __init__(self, document):
        self.document = document

    def load_manifest(self):
        return {
            "meta": self.document.meta,
            "source_lang": self.document.source_lang,
            "target_lang": self.document.target_lang,
            "chapters": [{"index": chapter.index} for chapter in self.document.chapters],
        }

    def load_chapter(self, index):
        return self.document.chapters[index]


class TestEpubStage1(unittest.TestCase):
    def _book(self, xhtml: bytes):
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as handle:
            path = handle.name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", _CONTAINER)
            archive.writestr("O/content.opf", _OPF)
            archive.writestr("O/c.xhtml", xhtml)
        self.addCleanup(os.unlink, path)
        return path

    def test_slots_preserve_topology_attributes_and_vertical_layout(self):
        source = b"""<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head/><body style="writing-mode: vertical-rl" class="vrtl"><p id="p" class="keep" style="color:red"> A <em id="e">B</em> C<img src="x.png"/> D</p></body></html>"""
        path = self._book(source)
        document = read_epub(path, "en", "zh")
        segment = document.chapters[0].segments[0]
        segment.assign_translation(
            [
                {
                    "id": slot.id,
                    "value": f"译{i}" if slot.source_value.strip() else "",
                }
                for i, slot in enumerate(segment.epub_state.slots)
            ],
        )
        output = path + ".out.epub"
        self.addCleanup(os.unlink, output)
        assemble_source_epub(_Store(document), path, output, target_lang="zh-Hans")
        with zipfile.ZipFile(output) as archive:
            rendered = archive.read("O/c.xhtml")
        root = etree.fromstring(rendered)
        paragraph = root.find(".//{http://www.w3.org/1999/xhtml}p")
        self.assertIsNotNone(paragraph)
        self.assertEqual(paragraph.get("class"), "keep")
        self.assertEqual(paragraph.get("style"), "color:red")
        self.assertIn(b"writing-mode: vertical-rl", rendered)
        self.assertNotIn(b"data-tn-", rendered)
        self.assertEqual(len(paragraph), 2)

    def test_translation_changes_only_authorized_text_and_tail_slots(self):
        source = (
            b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            b'<body><p id="p" class="keep" style="color:red">Alpha <em id="e">Beta</em>'
            b' Gamma<a id="a" href="https://example.test">Delta</a> Epsilon</p></body></html>'
        )
        path = self._book(source)
        document = read_epub(path, "en", "zh")
        segment = document.chapters[0].segments[0]
        original_root = etree.fromstring(source)

        def snapshot(root):
            result = {}

            def visit(node, path=(), parent_path=None):
                result[path] = (
                    node.tag,
                    parent_path,
                    tuple(node.attrib.items()),
                    tuple(child.tag for child in node if isinstance(child.tag, str)),
                )
                for index, child in enumerate(
                    child for child in node if isinstance(child.tag, str)
                ):
                    visit(child, (*path, index), path)

            visit(root)
            return result

        before_structure = snapshot(original_root)
        segment.assign_translation(
            [
                {
                    "id": slot.id,
                    "value": f"译{i}" if slot.source_value.strip() else "",
                }
                for i, slot in enumerate(segment.epub_state.slots)
            ],
        )
        output = path + ".topology.epub"
        self.addCleanup(os.unlink, output)
        assemble_source_epub(_Store(document), path, output, target_lang="zh")
        with zipfile.ZipFile(output) as archive:
            rendered = etree.fromstring(archive.read("O/c.xhtml"))
        self.assertEqual(snapshot(rendered), before_structure)
        self.assertEqual(
            [child.tag for child in rendered.find("{http://www.w3.org/1999/xhtml}body")],
            ["{http://www.w3.org/1999/xhtml}p"],
        )
        paragraph = rendered.find(".//{http://www.w3.org/1999/xhtml}p")
        self.assertIsNotNone(paragraph)
        assert paragraph is not None
        self.assertEqual(paragraph.get("id"), "p")
        self.assertEqual(paragraph.get("class"), "keep")
        self.assertEqual(paragraph.get("style"), "color:red")
        link = paragraph.find("{http://www.w3.org/1999/xhtml}a")
        self.assertIsNotNone(link)
        assert link is not None
        self.assertEqual(link.get("href"), "https://example.test")
        self.assertEqual(link.get("id"), "a")
        self.assertNotIn(b"data-tn-", etree.tostring(rendered))

        def resolve(root, path):
            current = root
            for index in path:
                current = [child for child in current if isinstance(child.tag, str)][index]
            return current

        source_block = resolve(original_root, segment.epub_state.block_path)
        rendered_block = resolve(rendered, segment.epub_state.block_path)
        for slot in segment.epub_state.slots:
            before_owner = resolve(source_block, slot.element_path)
            after_owner = resolve(rendered_block, slot.element_path)
            before_value = getattr(before_owner, slot.field)
            after_value = getattr(after_owner, slot.field)
            self.assertEqual(before_value, slot.source_value)
            self.assertEqual(
                after_value,
                slot.target_value,
            )

    def test_whitespace_tail_inside_inline_pagebreak_run_is_persisted(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            b"<p>One<em>two</em> <span>Next</span><br/>After</p></body></html>"
        )
        document = read_epub(path, "en", "zh")
        first, second = document.chapters[0].segments
        self.assertEqual(first.source, "Onetwo Next")
        self.assertEqual(second.source, "After")
        self.assertEqual(
            [(slot.element_path, slot.field, slot.source_value) for slot in first.epub_state.slots],
            [
                ((), "text", "One"),
                ((0,), "text", "two"),
                ((0,), "tail", " "),
                ((1,), "text", "Next"),
            ],
        )

    def test_schema4_runstore_save_load_preserves_slot_state(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Alpha <em>Beta</em></p></body></html>"
        )
        document = read_epub(path, "en", "zh")
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(os.path.join(directory, "run"))
            manifest = store.stage_document(
                document,
                RunIdentity(
                    source_bytes_sha256=document.meta["epub_sha256"],
                    source_lang="en",
                    target_lang="zh",
                ),
            )
            store.save_state(RunState.model_validate(manifest))
            reopened = RunStore(store.run_dir)
            loaded = reopened.load_chapter(0).segments[0]
            self.assertEqual(reopened.load_manifest()["meta"]["epub_schema"], 4)
            self.assertEqual(
                [slot.id for slot in loaded.epub_state.slots],
                [slot.id for slot in document.chapters[0].segments[0].epub_state.slots],
            )
            self.assertEqual(loaded.source, "Alpha Beta")

    def test_punctuation_normalization_keeps_ids_and_crosses_slot_boundaries(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Alpha <em>Beta</em></p></body></html>"
        )
        segment = read_epub(path, "en", "zh").chapters[0].segments[0]
        transport = [
            {"id": slot.id, "value": value}
            for slot, value in zip(segment.epub_state.slots, ["“甲,", "乙..."], strict=True)
        ]
        normalized = normalize_slot_transport(segment.epub_state, transport)
        self.assertEqual(
            [item["id"] for item in normalized],
            [slot.id for slot in segment.epub_state.slots],
        )
        flattened = "".join(item["value"] for item in normalized)
        self.assertEqual(flattened, normalize_zh("“甲,乙..."))
        self.assertEqual(normalize_zh_parts(["“甲,", "乙..."]), ["“甲，", "乙……"])

    def test_split_ellipsis_and_dash_runs_remain_nonempty_per_slot(self):
        for values, expected in (([".", ".."], ["…", "…"]), (["-", "-"], ["—", "—"])):
            with self.subTest(values=values):
                path = self._book(
                    b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Alpha<em>Beta</em></p></body></html>"
                )
                segment = read_epub(path, "en", "zh").chapters[0].segments[0]
                transport = [
                    {"id": slot.id, "value": value}
                    for slot, value in zip(segment.epub_state.slots, values, strict=True)
                ]
                normalized = normalize_slot_transport(segment.epub_state, transport)
                segment.assign_translation(normalized)
                self.assertEqual(
                    [item["id"] for item in normalized],
                    [slot.id for slot in segment.epub_state.slots],
                )
                self.assertEqual([item["value"] for item in normalized], expected)
                self.assertEqual("".join(item["value"] for item in normalized), "".join(expected))
                self.assertEqual(segment.target, "".join(expected))

    def test_invalid_slot_assignment_does_not_collapse_or_mutate(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Alpha<em>Beta</em></p></body></html>"
        )
        segment = read_epub(path, "en", "zh").chapters[0].segments[0]
        invalid = [{"id": slot.id, "value": "."} for slot in reversed(segment.epub_state.slots)]
        with self.assertRaisesRegex(ValueError, "IDs/order"):
            segment.assign_translation(invalid)
        self.assertIsNone(segment.target)
        self.assertTrue(all(slot.target_value is None for slot in segment.epub_state.slots))

    def test_quote_normalization_shares_state_across_inline_slots(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Alpha <em>Beta</em></p></body></html>"
        )
        segment = read_epub(path, "en", "zh").chapters[0].segments[0]
        transport = [
            {"id": slot.id, "value": value}
            for slot, value in zip(segment.epub_state.slots, ['"甲', '乙"'], strict=True)
        ]
        normalized = normalize_slot_transport(segment.epub_state, transport)
        segment.assign_translation(normalized)
        self.assertEqual([slot.target_value for slot in segment.epub_state.slots], ["“甲", "乙”"])
        self.assertEqual(segment.target, "“甲乙”")

    def test_unmarked_superscript_links_remain_in_translation_source(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Lead "
            b"<sup><a href='#x1'>1</a></sup> tail <span class='footnote'><sup>"
            b"<a href='#x2'>2</a></sup></span></p></body></html>"
        )
        segment = read_epub(path, "en", "zh").chapters[0].segments[0]
        self.assertEqual(segment.source, "Lead 1 tail 2")

    def test_direct_br_slots_write_each_line_once(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            b"<p>Line one<br/>Line two</p></body></html>"
        )
        document = read_epub(path, "en", "zh")
        segments = document.chapters[0].segments
        for index, segment in enumerate(segments):
            segment.assign_translation(
                [
                    {
                        "id": slot.id,
                        "value": f"译{index}" if slot.source_value.strip() else "",
                    }
                    for slot in segment.epub_state.slots
                ],
            )
        output = path + ".br.epub"
        self.addCleanup(os.unlink, output)
        assemble_source_epub(_Store(document), path, output, target_lang="zh")
        with zipfile.ZipFile(output) as archive:
            root = etree.fromstring(archive.read("O/c.xhtml"))
        paragraph = root.find(".//{http://www.w3.org/1999/xhtml}p")
        self.assertIsNotNone(paragraph)
        assert paragraph is not None
        self.assertEqual(paragraph.text, "译0")
        self.assertEqual(paragraph[0].tail, "译1")
        self.assertEqual(etree.tostring(paragraph, encoding="unicode").count("译"), 2)

    def test_malformed_resource_records_recovered_mode(self):
        path = self._book(b"<html><body><p>broken <em>text</p></body></html>")
        document = read_epub(path, "en", "zh")
        resource = document.meta["epub_resources"][0]
        self.assertEqual(resource["parse_mode"], "recovered")
        self.assertNotEqual(resource["parser_diagnostics"], [])

    def test_stale_archive_fails_closed(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Text</p></body></html>"
        )
        document = read_epub(path, "en", "zh")
        with open(path, "ab") as stream:
            stream.write(b"stale")
        with self.assertRaisesRegex(ValueError, "archive digest"):
            assemble_source_epub(_Store(document), path, path + ".out.epub", target_lang="zh")

    def test_adapter_keeps_plain_target_synchronized(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>A <em>B</em></p></body></html>"
        )
        document = read_epub(path, "en", "zh")
        segment = document.chapters[0].segments[0]
        segment.assign_translation(
            [
                {"id": slot.id, "value": "译" if slot.source_value.strip() else ""}
                for slot in segment.epub_state.slots
            ],
        )
        self.assertEqual(segment.target, "译译")
        self.assertEqual([slot.target_value for slot in segment.epub_state.slots], ["译", "译"])

    def test_immutable_nodes_and_comment_pi_tails_are_preserved(self):
        source = (
            b"<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml' "
            b"xmlns:epub='http://www.idpf.org/2007/ops'><body>"
            b"<p>A<!--keep--> tail<?pagebreak x?> <ruby>\xe6\xbc\xa2\xe5\xad\x97<rt>\xe3\x81\x8b\xe3\x82\x93\xe3\x81\x98</rt></ruby>"
            b"<svg><text>SVG</text></svg><math><mi>M</mi></math>"
            b"<sup><a epub:type='noteref' href='#n'>1</a></sup></p></body></html>"
        )
        path = self._book(source)
        document = read_epub(path, "en", "zh")
        segment = document.chapters[0].segments[0]
        self.assertTrue(all("1" not in slot.source_value for slot in segment.epub_state.slots))
        segment.assign_translation(
            [
                {"id": slot.id, "value": "译" if slot.source_value.strip() else ""}
                for slot in segment.epub_state.slots
            ],
        )
        output = path + ".out.epub"
        self.addCleanup(os.unlink, output)
        assemble_source_epub(_Store(document), path, output, target_lang="zh")
        with zipfile.ZipFile(output) as archive:
            rendered = archive.read("O/c.xhtml")
        self.assertIn(b"keep", rendered)
        self.assertIn(b"pagebreak", rendered)
        self.assertIn(b"SVG", rendered)
        self.assertIn(b"noteref", rendered)
        self.assertIn(b"\xe3\x81\x8b\xe3\x82\x93\xe3\x81\x98", rendered)

    def test_stale_slot_contract_and_transport_fail_closed(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Text</p></body></html>"
        )
        document = read_epub(path, "en", "zh")
        segment = document.chapters[0].segments[0]
        with self.assertRaisesRegex(ValueError, "IDs/order"):
            validate_slot_transport(segment.epub_state, [{"id": "unknown", "value": "译"}])
        segment.assign_translation(
            [{"id": slot.id, "value": "译"} for slot in segment.epub_state.slots],
        )
        segment.epub_state.slots.pop()
        with self.assertRaisesRegex(ValueError, "contract digest"):
            assemble_source_epub(_Store(document), path, path + ".out.epub", target_lang="zh")
        if os.path.exists(path + ".out.epub"):
            os.unlink(path + ".out.epub")

    def test_state_round_trip_and_schema_gate(self):
        path = self._book(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>A <em>B</em> C</p></body></html>"
        )
        document = read_epub(path, "en", "zh")
        restored = type(document).model_validate(document.model_dump(mode="json"))
        state = restored.chapters[0].segments[0].epub_state
        self.assertIsNotNone(state)
        self.assertEqual(
            state.slot_contract_sha256,
            document.chapters[0].segments[0].epub_state.slot_contract_sha256,
        )
        restored.meta["epub_schema"] = 2
        with self.assertRaisesRegex(ValueError, "fresh translation"):
            assemble_source_epub(_Store(restored), path, path + ".schema2.epub", target_lang="zh")


class TestPreservedChapterRanges(unittest.TestCase):
    _book = TestEpubStage1._book

    def test_shared_resource_preserves_only_declared_chapter_ranges(self):
        source = (
            b'<html xmlns="http://www.w3.org/1999/xhtml" lang="fr"><head/>'
            b'<body><p lang="en">Story Beta.</p>'
            b'<p>Reference <span lang="de">Alpha</span>.</p></body></html>'
        )
        path = self._book(source)
        document = read_epub(path, "en", "zh")
        story, reference = document.chapters[0].segments
        for segment, value in ((story, "故事译文"), (reference, "错误改写")):
            segment.assign_translation(
                [
                    {"id": slot.id, "value": value if slot.source_value.strip() else ""}
                    for slot in segment.epub_state.slots
                ]
            )
        preserve = ChapterProcessing(
            action="preserve",
            review_required=False,
            reason="reference list",
            source_sha256="source",
            strategy_version="chapter_semantics_v1",
        )
        translate = ChapterProcessing(
            action="translate",
            review_required=False,
            reason="narrative",
            source_sha256="source",
            strategy_version="chapter_semantics_v1",
        )
        document.chapters = [
            Chapter(index=0, title="Story", segments=[story], processing=translate),
            Chapter(index=1, title="Reference", segments=[reference], processing=preserve),
        ]
        output = path + ".preserved.epub"
        self.addCleanup(os.unlink, output)
        store = _Store(document)
        assemble_source_epub(store, path, output, target_lang="zh")
        with zipfile.ZipFile(output) as archive:
            root = etree.fromstring(archive.read("O/c.xhtml"))
        paragraphs = root.findall(".//{http://www.w3.org/1999/xhtml}p")
        self.assertEqual("".join(paragraphs[0].itertext()), "故事译文")
        self.assertEqual("".join(paragraphs[1].itertext()), "Reference Alpha.")
        self.assertEqual(paragraphs[1].get("{http://www.w3.org/XML/1998/namespace}lang"), "fr")
        self.assertEqual(paragraphs[1][0].get("lang"), "de")
        self.assertEqual(root.get("lang"), "zh")

        from trans_novel.assemble.epub.verification import verify_epub

        for order in ("target_first", "source_first"):
            bilingual_output = path + f".{order}.epub"
            self.addCleanup(os.unlink, bilingual_output)
            assemble_source_epub(
                store,
                path,
                bilingual_output,
                target_lang="zh",
                bilingual=True,
                order=order,
            )
            with zipfile.ZipFile(bilingual_output) as archive:
                bilingual_root = etree.fromstring(archive.read("O/c.xhtml"))
            self.assertEqual("".join(bilingual_root.itertext()).count("Reference Alpha."), 1)
            self.assertEqual(
                len(
                    bilingual_root.xpath(
                        '//*[contains(concat(" ", normalize-space(@class), " "), " tn-source ")]'
                    )
                ),
                1,
            )
            self.assertTrue(
                verify_epub(
                    bilingual_output,
                    source_path=path,
                    store=store,
                    mode="bilingual",
                    bilingual=True,
                    target_lang="zh",
                    bilingual_order=order,
                )["passed"]
            )

        paragraphs[1].text = "tampered"
        with zipfile.ZipFile(output) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
        tampered = output + ".tmp"
        with zipfile.ZipFile(tampered, "w") as archive:
            for info, data in entries:
                archive.writestr(
                    info,
                    etree.tostring(root, encoding="utf-8")
                    if info.filename == "O/c.xhtml"
                    else data,
                )
        os.replace(tampered, output)
        report = verify_epub(
            output,
            source_path=path,
            store=store,
            mode="monolingual",
            bilingual=False,
            target_lang="zh",
        )
        self.assertIn("slot_value_mismatch", {item["code"] for item in report["failures"]})


class TestPreservedLanguageRanges(unittest.TestCase):
    _book = TestEpubStage1._book

    def test_language_fallback_and_explicit_empty_survive_mixed_resource(self):
        source = (
            b'<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>'
            b'<p>Story.</p><p>Fallback language.</p><p lang="">Unknown language.</p>'
            b"</body></html>"
        )
        path = self._book(source)
        document = read_epub(path, "en", "zh")
        story, fallback, unknown = document.chapters[0].segments
        for segment, value in (
            (story, "故事。"),
            (fallback, "错误"),
            (unknown, "错误"),
        ):
            segment.assign_translation(
                [
                    {"id": slot.id, "value": value if slot.source_value.strip() else ""}
                    for slot in segment.epub_state.slots
                ]
            )
        document.chapters = [
            Chapter(
                index=0,
                segments=[story],
                processing=ChapterProcessing(
                    action="translate",
                    review_required=False,
                    reason="narrative",
                    source_sha256="source",
                    strategy_version="chapter_semantics_v1",
                ),
            ),
            Chapter(
                index=1,
                segments=[fallback, unknown],
                processing=ChapterProcessing(
                    action="preserve",
                    review_required=False,
                    reason="reference list",
                    source_sha256="source",
                    strategy_version="chapter_semantics_v1",
                ),
            ),
        ]
        output = path + ".languages.epub"
        self.addCleanup(os.unlink, output)
        store = _Store(document)
        assemble_source_epub(store, path, output, target_lang="zh")

        with zipfile.ZipFile(output) as archive:
            root = etree.fromstring(archive.read("O/c.xhtml"))
        paragraphs = root.findall(".//{http://www.w3.org/1999/xhtml}p")
        self.assertEqual(paragraphs[1].get("{http://www.w3.org/XML/1998/namespace}lang"), "en")
        self.assertEqual(paragraphs[2].get("lang"), "")
        self.assertIsNone(paragraphs[2].get("{http://www.w3.org/XML/1998/namespace}lang"))

        from trans_novel.assemble.epub.verification import verify_epub

        self.assertTrue(
            verify_epub(
                output,
                source_path=path,
                store=store,
                mode="monolingual",
                bilingual=False,
                target_lang="zh",
            )["passed"]
        )


class TestPreservedNavigation(unittest.TestCase):
    def test_nav_and_ncx_preserve_nested_labels_for_preserved_chapter(self):
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as handle:
            path = handle.name
        self.addCleanup(os.unlink, path)
        write_phase9_epub(path, long_chapter_chars=50)
        with zipfile.ZipFile(path) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
        with zipfile.ZipFile(path, "w") as archive:
            for info, data in entries:
                if info.filename == "OEBPS/nav.xhtml":
                    data = data.replace(
                        b"</a></li>",
                        b'</a><ol><li><a href="text/chapter-1.xhtml#intro">'
                        b"Reference Detail</a></li></ol></li>",
                        1,
                    )
                elif info.filename == "OEBPS/toc.ncx":
                    data = data.replace(
                        b"</navPoint>",
                        b'<navPoint id="detail"><navLabel><text>Reference Detail</text>'
                        b'</navLabel><content src="text/chapter-1.xhtml#intro"/>'
                        b"</navPoint></navPoint>",
                        1,
                    )
                archive.writestr(info, data)

        document = read_epub(path, "en", "zh")
        for chapter in document.chapters:
            chapter.processing = ChapterProcessing(
                action="preserve" if chapter.index == 0 else "translate",
                review_required=False,
                reason="reference list" if chapter.index == 0 else "narrative",
                source_sha256="source",
                strategy_version="chapter_semantics_v1",
            )
            for segment in chapter.segments:
                segment.assign_translation(
                    [
                        {
                            "id": slot.id,
                            "value": ("错误改写" if chapter.index == 0 else "译文")
                            if slot.source_value.strip()
                            else "",
                        }
                        for slot in segment.epub_state.slots
                    ]
                )
        for entry in document.meta["toc_entries"]:
            entry["title_translated"] = (
                "损坏明细"
                if entry["title"] == "Reference Detail"
                else "损坏标题"
                if entry["title"] == "Chapter One"
                else "第二章"
            )
        store = _Store(document)

        from trans_novel.assemble.epub.verification import verify_epub

        for bilingual, order in ((False, "target_first"), (True, "source_first")):
            output = path + f".nav-{bilingual}.epub"
            self.addCleanup(os.unlink, output)
            assemble_source_epub(
                store,
                path,
                output,
                target_lang="zh",
                bilingual=bilingual,
                order=order,
            )
            with zipfile.ZipFile(output) as archive:
                nav = archive.read("OEBPS/nav.xhtml").decode()
                ncx = archive.read("OEBPS/toc.ncx").decode()
            for markup in (nav, ncx):
                self.assertIn("Chapter One", markup)
                self.assertIn("Reference Detail", markup)
                self.assertIn("第二章", markup)
                self.assertNotIn("损坏", markup)
                self.assertIn("text/chapter-1.xhtml#intro", markup)
            self.assertTrue(
                verify_epub(
                    output,
                    source_path=path,
                    store=store,
                    mode="bilingual" if bilingual else "monolingual",
                    bilingual=bilingual,
                    target_lang="zh",
                    bilingual_order=order,
                )["passed"]
            )


if __name__ == "__main__":
    unittest.main()
