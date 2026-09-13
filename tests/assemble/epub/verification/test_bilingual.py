from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
from lxml import etree

from tests.fixtures.books import write_phase9_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble.epub.rendering import (
    BILINGUAL_SOURCE_CLASS,
)
from trans_novel.assemble.epub.rendering import (
    add_bilingual_sources as _add_bilingual_sources,
)
from trans_novel.assemble.epub.rendering import (
    bilingual_source_copy as _bilingual_source_copy,
)
from trans_novel.assemble.epub.verification import validate_epub, validate_epub_triplet, verify_epub
from trans_novel.assemble.epub.verification.bilingual import bilingual_proof
from trans_novel.assemble.epub.verification.dom import source_subtree_signature
from trans_novel.config import Config
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application


def _copy_epub(
    source: Path, target: Path, replacements: dict[str, bytes | None] | None = None
) -> None:
    replacements = replacements or {}
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(target, "w") as zout:
        existing = set()
        for info in zin.infolist():
            existing.add(info.filename)
            if info.filename in replacements and replacements[info.filename] is None:
                continue
            replacement = replacements.get(info.filename)
            data = replacement if replacement is not None else zin.read(info.filename)
            compression = (
                zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            )
            zout.writestr(info.filename, data, compression)
        for name, data in replacements.items():
            if name not in existing and data is not None:
                zout.writestr(name, data, zipfile.ZIP_DEFLATED)


def _config(state_dir: str):
    config = Config.from_dict({"llm": fake_llm_dict(), "quality": "quality"})
    config.source_lang = "ja"
    config.state_dir = state_dir
    return config


def _run(input_path, state_dir):
    cfg = _config(state_dir)
    orch = Application(cfg, client=FakeClient(handler=routing_handler))
    store = orch.run(input_path)
    _stamp_formal_prereqs(store)
    return store, cfg


def _stamp_formal_prereqs(store):
    """Direct writer tests stamp title, QA, Repair, and report prerequisites."""
    from trans_novel.pipeline.state import NODE_DETERMINISTIC_QA, NODE_REPAIR, NodeState

    state = store.load_state()
    for node_id in ("titles", NODE_DETERMINISTIC_QA, NODE_REPAIR, "report"):
        state.nodes.setdefault(node_id, NodeState(node_id=node_id, status="succeeded"))
    store.save_state(state)
    return store


class TestEpubStage2(unittest.TestCase):
    def test_real_schema4_bilingual_report_proves_both_orders(self) -> None:
        from trans_novel.assemble import assemble

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            store, _ = _run(str(source), str(root / "state"))
            for order in ("target_first", "source_first"):
                output = root / f"{order}.epub"
                assemble(
                    store,
                    str(source),
                    str(output),
                    out_format="epub",
                    bilingual=True,
                    order=order,
                )
                report = store.load_epub_verification()
                assert report is not None
                self.assertTrue(report["passed"])
                self.assertEqual(report["authorized_differences"]["bilingual_nodes"], 12)

    def test_attributed_wrapped_japanese_ruby_source_passes_canonical_proof(self) -> None:
        source = etree.fromstring(
            "<html><head></head><body><p><em>前<ruby id='drop' class='keep'>"
            "<rb data-note='keep'>漢</rb><rt>かん</rt><rp>（</rp></ruby>後</em></p>"
            "</body></html>".encode()
        )
        output = etree.fromstring(
            '<html><head></head><body><p>译文</p><p class="tn-source ibooks-dark-theme-use-custom-text-color">'
            "前<ruby class='keep'><rb>漢</rb><rt>かん</rt><rp>（</rp></ruby>後"
            "</p></body></html>".encode()
        )
        segment = SimpleNamespace(
            kind="text",
            source="前漢後",
            target="译文",
            epub_state=SimpleNamespace(block_path=(1, 0)),
        )

        expected = _bilingual_source_copy(
            source.find(".//p"),
            source.find(".//p"),
            source_lang="ja",
            source_tag="p",
        )
        actual = output.xpath(".//p[@class]")[0]
        self.assertEqual(source_subtree_signature(expected), source_subtree_signature(actual))
        failures: list[dict[str, str]] = []
        bilingual_proof(
            source,
            output,
            [segment],
            source_lang="ja",
            order="target_first",
            resource="chapter.xhtml",
            failures=failures,
        )
        self.assertNotIn("source_node_subtree_mismatch", {item["code"] for item in failures})
        self.assertNotIn("source_node_ruby_mismatch", {item["code"] for item in failures})

    def test_bilingual_source_active_media_and_attributes_are_checked(self) -> None:
        source = etree.fromstring(b"<html><body><p>Original</p></body></html>")
        output = etree.fromstring(
            b'<html><body><p>Translated</p><p class="tn-source">'
            b"<img src='active.png'/>Original</p></body></html>"
        )
        state = SimpleNamespace(block_path=(0, 0))
        segment = SimpleNamespace(
            kind="text",
            source="Original",
            target="Translated",
            epub_state=state,
        )
        failures: list[dict[str, str]] = []
        count = bilingual_proof(
            source,
            output,
            [segment],
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=failures,
        )
        self.assertEqual(count, 1)
        self.assertIn("source_node_attributes", {item["code"] for item in failures})
        self.assertIn("source_node_active_media", {item["code"] for item in failures})

    def test_repeated_source_blocks_match_distinct_adjacent_targets(self) -> None:
        source = etree.fromstring(
            b"<html><head></head><body><p>Same</p><p>Middle</p><p>Same</p></body></html>"
        )
        output = etree.fromstring(etree.tostring(source))
        paths = ((1, 0), (1, 1), (1, 2))
        segments = [
            SimpleNamespace(
                kind="text",
                source=text,
                target=target,
                epub_state=SimpleNamespace(block_path=path, slots=[]),
            )
            for path, text, target in zip(
                paths, ("Same", "Middle", "Same"), ("同一", "中间", "同一"), strict=True
            )
        ]
        blocks = source.xpath(".//p")
        output_blocks = output.xpath(".//p")
        _add_bilingual_sources(
            output,
            segments,
            order="target_first",
            source_blocks={
                path: etree.fromstring(etree.tostring(block))
                for path, block in zip(paths, blocks, strict=True)
            },
            block_refs=dict(zip(paths, output_blocks, strict=True)),
        )
        failures: list[dict[str, str]] = []
        bilingual_proof(
            source,
            output,
            segments,
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=failures,
        )
        self.assertEqual(failures, [])

    def test_container_atomic_media_source_is_sanitized_both_orders(self) -> None:
        source = etree.fromstring(
            b"<html><head></head><body><li>Before<svg><text>DROP</text></svg>After</li></body></html>"
        )
        block = source.xpath(".//li")[0]
        segment = SimpleNamespace(
            kind="text",
            source="BeforeAfter",
            target="Translated",
            epub_state=SimpleNamespace(block_path=(1, 0), slots=[]),
        )
        for order in ("target_first", "source_first"):
            output = etree.fromstring(etree.tostring(source))
            output_block = output.xpath(".//li")[0]
            output_block.text = "Translated"
            self.assertEqual(
                _add_bilingual_sources(
                    output,
                    [segment],
                    order=order,
                    source_blocks={(1, 0): etree.fromstring(etree.tostring(block))},
                    block_refs={(1, 0): output_block},
                ),
                1,
            )
            source_node = output.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' tn-source ')]"
            )[0]
            self.assertEqual("".join(source_node.itertext()), "BeforeAfter")
            self.assertFalse(source_node.xpath(".//*[local-name()='svg' or local-name()='text']"))
            failures: list[dict[str, str]] = []
            bilingual_proof(
                source,
                output,
                [segment],
                source_lang="en",
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertNotIn("source_target_pair_mismatch", {item["code"] for item in failures})

    def test_direct_run_sources_keep_two_slots_in_same_active_boundary_order(self) -> None:
        markup = b"<html><head></head><body><p><a href='x'><em>One</em><em>Two</em></a><br/>Tail</p></body></html>"
        source = etree.fromstring(markup)
        slots = [
            SimpleNamespace(
                element_path=(0, 0),
                field="text",
                source_value="One",
                target_value="Uno",
            ),
            SimpleNamespace(
                element_path=(0, 1),
                field="text",
                source_value="Two",
                target_value="Dos",
            ),
            SimpleNamespace(
                element_path=(1,),
                field="tail",
                source_value="Tail",
                target_value="尾",
            ),
        ]
        segment = SimpleNamespace(
            kind="text",
            source="One Two Tail",
            target="Uno Dos 尾",
            epub_state=SimpleNamespace(block_path=(1, 0), slots=slots),
        )
        for order in ("target_first", "source_first"):
            output = etree.fromstring(markup)
            output_block = output.xpath(".//p")[0]
            _add_bilingual_sources(
                output,
                [segment],
                order=order,
                source_blocks={(1, 0): etree.fromstring(markup).xpath(".//p")[0]},
                block_refs={(1, 0): output_block},
            )
            source_texts = [
                node.text
                for node in output.xpath(
                    ".//*[contains(concat(' ', normalize-space(@class), ' '), ' tn-source ')]"
                )
            ]
            self.assertEqual(source_texts, ["One", "Two", "Tail"])
            failures: list[dict[str, str]] = []
            bilingual_proof(
                source,
                output,
                [segment],
                source_lang="en",
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertNotIn("source_node_order", {item["code"] for item in failures})

        corrupt = etree.fromstring(markup)
        corrupt_block = corrupt.xpath(".//p")[0]
        _add_bilingual_sources(
            corrupt,
            [segment],
            order="target_first",
            source_blocks={(1, 0): etree.fromstring(markup).xpath(".//p")[0]},
            block_refs={(1, 0): corrupt_block},
        )
        first = next(
            node
            for node in corrupt_block
            if node.get("class") == BILINGUAL_SOURCE_CLASS and node.text == "One"
        )
        second = next(
            node
            for node in corrupt_block
            if node.get("class") == BILINGUAL_SOURCE_CLASS and node.text == "Two"
        )
        corrupt_block.remove(first)
        corrupt_block.remove(second)
        anchor = corrupt_block.index(corrupt_block.xpath("./a")[0])
        corrupt_block.insert(anchor + 1, second)
        corrupt_block.insert(anchor + 2, first)
        failures = []
        bilingual_proof(
            source,
            corrupt,
            [segment],
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=failures,
        )
        self.assertIn("source_node_order", {item["code"] for item in failures})


class TestBilingualSourceFailures(unittest.TestCase):
    def test_unknown_namespace_source_wrapper_preserves_visible_pairing_text(self) -> None:
        source = etree.fromstring(
            b"<html xmlns:u='urn:unknown'><head></head><body><p>Before"
            b"<u:wrapper>Visible<em>Inner</em></u:wrapper>After</p></body></html>"
        )
        segment = SimpleNamespace(
            kind="text",
            source="BeforeVisibleInnerAfter",
            target="Translated",
            epub_state=SimpleNamespace(block_path=(1, 0)),
        )
        output = etree.fromstring(
            b'<html><head></head><body><p>Translated</p><p class="tn-source '
            b'ibooks-dark-theme-use-custom-text-color">BeforeVisible<em>Inner</em>After</p>'
            b"</body></html>"
        )
        failures: list[dict[str, str]] = []
        bilingual_proof(
            source,
            output,
            [segment],
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=failures,
        )
        self.assertNotIn("source_node_subtree_mismatch", {item["code"] for item in failures})

    def test_corrupt_ruby_source_subtree_fails_exactly(self) -> None:
        source = etree.fromstring(
            b"<html><body><p><ruby>\xe6\xbc\xa2<rt>\xe3\x81\x8b\xe3\x82\x93</rt></ruby></p></body></html>"
        )
        state = SimpleNamespace(block_path=(0, 0))
        segment = SimpleNamespace(
            kind="text",
            source="\u6f22",
            target="\u8bd1",
            epub_state=state,
        )
        outputs = (
            b'<html><body><p>\xe8\xaf\x91</p><p class="tn-source ibooks-dark-theme-use-custom-text-color">'
            b"<ruby>\xe6\xbc\xa2<rt>\xe6\x94\xb9</rt></ruby></p></body></html>",
            b'<html><body><p>\xe8\xaf\x91</p><p class="tn-source ibooks-dark-theme-use-custom-text-color">'
            b"<ruby><rb>\xe6\xbc\xa2</rb><rt>\xe3\x81\x8b\xe3\x82\x93</rt></ruby></p></body></html>",
        )

        for data in outputs:
            failures: list[dict[str, str]] = []
            bilingual_proof(
                source,
                etree.fromstring(data),
                [segment],
                source_lang="ja",
                order="target_first",
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertTrue(
                {
                    "source_node_ruby_mismatch",
                    "source_node_subtree_mismatch",
                }
                & {item["code"] for item in failures}
            )

    def test_generated_mono_rejects_stray_tn_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "generated.epub"
            write_phase9_epub(str(source))
            with zipfile.ZipFile(source) as zin:
                entries = {info.filename: zin.read(info.filename) for info in zin.infolist()}
            entries["OEBPS/text/chapter-1.xhtml"] = entries["OEBPS/text/chapter-1.xhtml"].replace(
                b"</body>", b'<p class="tn-source">stray</p></body>'
            )
            with zipfile.ZipFile(source, "w") as zout:
                for name, data in entries.items():
                    zout.writestr(
                        name,
                        data,
                        zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED,
                    )
            report = verify_epub(source, mode="generated", bilingual=False)
            self.assertIn("unexpected_source_nodes", {item["code"] for item in report["failures"]})

    def test_generated_bilingual_rejects_missing_source_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "generated.epub"
            write_phase9_epub(str(source))
            report = verify_epub(source, mode="generated", bilingual=True)
            self.assertTrue(report["failures"])
            self.assertIn(
                "missing_source_nodes",
                {item["code"] for item in report["failures"]},
            )

    def test_multi_base_ruby_direct_run_does_not_duplicate_source_values(self) -> None:
        source = etree.fromstring(
            "<html><head></head><body><p><ruby><rb>漢</rb><rt>かん</rt>"
            "<rb>字</rb><rt>じ</rt></ruby>です<br/>次</p></body></html>".encode()
        )
        slots = [
            SimpleNamespace(
                element_path=(0, 0),
                field="text",
                source_value="漢",
                target_value="Kan",
            ),
            SimpleNamespace(
                element_path=(0, 2),
                field="text",
                source_value="字",
                target_value="Ji",
            ),
            SimpleNamespace(
                element_path=(0,),
                field="tail",
                source_value="です",
                target_value="Desu",
            ),
            SimpleNamespace(
                element_path=(1,),
                field="tail",
                source_value="次",
                target_value="Next",
            ),
        ]
        segment = SimpleNamespace(
            kind="text",
            source="source",
            target="target",
            epub_state=SimpleNamespace(block_path=(1, 0), slots=slots),
        )
        template = etree.tostring(source)
        for order in ("target_first", "source_first"):
            output = etree.fromstring(template)
            block = output.xpath(".//p")[0]
            _add_bilingual_sources(
                output,
                [segment],
                order=order,
                source_blocks={(1, 0): etree.fromstring(template).xpath(".//p")[0]},
                block_refs={(1, 0): block},
            )
            source_nodes = output.xpath(".//*[contains(@class, 'tn-source')]")
            self.assertEqual(len(source_nodes), 3)
            values = ["".join(node.itertext()) for node in source_nodes]
            combined = "".join(values)
            self.assertIn("です", values)
            self.assertEqual(len(source.xpath(".//ruby")), 1)
            self.assertEqual(sum(len(node.xpath(".//ruby")) for node in source_nodes), 1)
            self.assertEqual(sum(len(node.xpath(".//rt")) for node in source_nodes), 2)
            self.assertEqual(combined.count("漢"), 1)
            self.assertEqual(combined.count("字"), 1)
            failures = []
            bilingual_proof(
                etree.fromstring(template),
                output,
                [segment],
                source_lang="ja",
                order=order,
                resource="test.xhtml",
                failures=failures,
            )
            self.assertEqual(failures, [])

    def test_bilingual_missing_dummy_and_misplaced_nodes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            mono = Path(directory) / "mono.epub"
            missing = Path(directory) / "missing.epub"
            dummy = Path(directory) / "dummy.epub"
            misplaced = Path(directory) / "misplaced.epub"
            write_phase9_epub(str(source))
            with zipfile.ZipFile(source) as zin:
                chapter_two = BeautifulSoup(zin.read("OEBPS/text/chapter-2.xhtml"), "xml")
                body = chapter_two.find(id="body-two")
                assert body is not None
                source_text = body.get_text(" ", strip=True)
                body.string = "Translated second chapter."
                translated_two = str(chapter_two).encode("utf-8")
                original_two = zin.read("OEBPS/text/chapter-2.xhtml")
                chapter_one = BeautifulSoup(zin.read("OEBPS/text/chapter-1.xhtml"), "xml")
                dummy_node = chapter_one.new_tag("p", attrs={"class": "tn-source"})
                dummy_node.string = source_text
                chapter_one.body.append(dummy_node)
                misplaced_one = str(chapter_one).encode("utf-8")
                dummy_soup = BeautifulSoup(translated_two, "xml")
                dummy_tag = dummy_soup.new_tag("p", attrs={"class": "tn-source"})
                dummy_tag.string = "not source"
                dummy_soup.body.append(dummy_tag)
                dummy_two = str(dummy_soup).encode("utf-8")
            _copy_epub(source, mono, {"OEBPS/text/chapter-2.xhtml": translated_two})
            _copy_epub(source, missing, {"OEBPS/text/chapter-2.xhtml": original_two})
            _copy_epub(source, dummy, {"OEBPS/text/chapter-2.xhtml": dummy_two})
            _copy_epub(
                source,
                misplaced,
                {
                    "OEBPS/text/chapter-2.xhtml": translated_two,
                    "OEBPS/text/chapter-1.xhtml": misplaced_one,
                },
            )
            self.assertFalse(validate_epub_triplet(source, mono, missing)["structural_pass"])
            self.assertFalse(validate_epub_triplet(source, mono, dummy)["structural_pass"])
            self.assertFalse(validate_epub_triplet(source, mono, misplaced)["structural_pass"])

    def test_zero_standalone_bilingual_source_nodes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            write_phase9_epub(str(source))
            result = validate_epub(source, bilingual=True)
            self.assertIn("missing_source_nodes", {item["code"] for item in result["failures"]})

    def test_reference_tag_attribute_and_multiplicity_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            output = Path(directory) / "output.epub"
            write_phase9_epub(str(source))
            with zipfile.ZipFile(source) as zin:
                chapter = zin.read("OEBPS/text/chapter-1.xhtml").replace(
                    b'<img src="../images/figure.png"',
                    b'<img src="../images/figure.png"/><img src="../images/figure.png"',
                    1,
                )
            _copy_epub(source, output, {"OEBPS/text/chapter-1.xhtml": chapter})
            result = validate_epub(output, source_path=source)
            self.assertIn("reference_graph_mismatch", {item["code"] for item in result["failures"]})
            self.assertGreater(result["counts"]["internal_links"]["checked"], 0)

    def test_mono_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            output = Path(directory) / "output.epub"
            write_phase9_epub(str(source))
            with zipfile.ZipFile(source) as zin:
                chapter = zin.read("OEBPS/text/chapter-1.xhtml").replace(
                    b'<p id="intro">', b'<p class="tn-source">source</p><p id="intro">'
                )
            _copy_epub(source, output, {"OEBPS/text/chapter-1.xhtml": chapter})
            codes = {
                item["code"]
                for item in validate_epub(output, source_path=source, bilingual=False)["failures"]
            }
            self.assertIn("unexpected_source_nodes", codes)

    def test_graph_removal_and_duplicate_multiplicity_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            removed = Path(directory) / "removed.epub"
            duplicated = Path(directory) / "duplicated.epub"
            write_phase9_epub(str(source))
            with zipfile.ZipFile(source) as zin:
                chapter = zin.read("OEBPS/text/chapter-1.xhtml")
                no_image = chapter.replace(b'<img src="../images/figure.png" alt="Figure"/>', b"")
                many_images = chapter.replace(
                    b'<img src="../images/figure.png" alt="Figure"/>',
                    b'<img src="../images/figure.png" alt="Figure"/><img src="../images/figure.png" alt="Figure"/>',
                )
            _copy_epub(source, removed, {"OEBPS/text/chapter-1.xhtml": no_image})
            _copy_epub(source, duplicated, {"OEBPS/text/chapter-1.xhtml": many_images})
            self.assertIn(
                "reference_graph_mismatch",
                {item["code"] for item in validate_epub(removed, source_path=source)["failures"]},
            )
            self.assertIn(
                "reference_graph_mismatch",
                {
                    item["code"]
                    for item in validate_epub(duplicated, source_path=source)["failures"]
                },
            )

    def test_direct_br_line_wrappers_validate_both_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.epub"
            source = Path(directory) / "source.epub"
            mono = Path(directory) / "mono.epub"
            write_phase9_epub(str(original))
            with zipfile.ZipFile(original) as zin:
                source_soup = BeautifulSoup(zin.read("OEBPS/text/chapter-2.xhtml"), "xml")
            source_body = source_soup.find(id="body-two")
            assert source_body is not None
            source_body.clear()
            source_body.append("Line one")
            source_body.append(source_soup.new_tag("br"))
            source_body.append("Line two")
            source_data = str(source_soup).encode("utf-8")
            mono_soup = BeautifulSoup(source_data, "xml")
            mono_body = mono_soup.find(id="body-two")
            assert mono_body is not None
            mono_body.clear()
            mono_body.append("Translated one")
            mono_body.append(mono_soup.new_tag("br"))
            mono_body.append("Translated two")
            mono_data = str(mono_soup).encode("utf-8")
            _copy_epub(original, source, {"OEBPS/text/chapter-2.xhtml": source_data})
            _copy_epub(original, mono, {"OEBPS/text/chapter-2.xhtml": mono_data})
            for order in ("target_first", "source_first"):
                bilingual = Path(directory) / f"bi-{order}.epub"
                bi_soup = BeautifulSoup(mono_data, "xml")
                bi_body = bi_soup.find(id="body-two")
                assert bi_body is not None
                bi_body.clear()
                for index, (target_text, source_text) in enumerate(
                    (("Translated one", "Line one"), ("Translated two", "Line two"))
                ):
                    if index:
                        bi_body.append(bi_soup.new_tag("br"))
                    target = bi_soup.new_tag("span")
                    target.string = target_text
                    source_span = bi_soup.new_tag("span", attrs={"class": "tn-source"})
                    source_span.string = source_text
                    if order == "target_first":
                        bi_body.extend([target, source_span])
                    else:
                        bi_body.extend([source_span, target])
                _copy_epub(
                    original,
                    bilingual,
                    {"OEBPS/text/chapter-2.xhtml": str(bi_soup).encode("utf-8")},
                )
                self.assertTrue(validate_epub_triplet(source, mono, bilingual)["structural_pass"])

    def test_inline_style_script_attribute_tamper_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            styled_source = Path(directory) / "styled-source.epub"
            preserved = Path(directory) / "preserved.epub"
            output = Path(directory) / "output.epub"
            write_phase9_epub(str(source))
            with zipfile.ZipFile(source) as zin:
                chapter = zin.read("OEBPS/text/chapter-1.xhtml").replace(
                    b"</head>", b'<style id="source-style" media="screen">x</style></head>'
                )
                tampered = chapter.replace(
                    b"</head>", b'<script type="text/javascript">x</script></head>'
                )
            _copy_epub(source, styled_source, {"OEBPS/text/chapter-1.xhtml": chapter})
            _copy_epub(styled_source, preserved)
            self.assertTrue(validate_epub(preserved, source_path=styled_source)["structural_pass"])
            _copy_epub(source, output, {"OEBPS/text/chapter-1.xhtml": tampered})
            codes = {item["code"] for item in validate_epub(output, source_path=source)["failures"]}
            self.assertIn("inline_resource_mismatch", codes)
