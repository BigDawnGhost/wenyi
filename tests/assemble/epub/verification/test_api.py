from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints

from lxml import etree

from tests.fixtures.books import write_phase9_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble.epub import verification as epub_verifier
from trans_novel.assemble.epub.rendering import BILINGUAL_SOURCE_CLASS
from trans_novel.assemble.epub.rendering.theme.contracts import ThemePlan
from trans_novel.assemble.epub.verification import verify_epub
from trans_novel.assemble.epub.verification.bilingual import _resolve_output_path, bilingual_proof
from trans_novel.assemble.epub.verification.navigation import nav_label_locations
from trans_novel.assemble.epub.verification.slots import compare_dom
from trans_novel.assemble.epub.verification.verify import _new_structural_failures
from trans_novel.config import Config
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application


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
    def test_source_missing_backlinks_are_inherited_by_count(self) -> None:
        inherited = {
            "category": "footnotes",
            "code": "missing_backlink",
            "path": "chapter.xhtml",
            "detail": "missing",
        }
        added = {**inherited, "path": "new.xhtml"}

        failures = _new_structural_failures(
            [inherited.copy(), inherited.copy(), added],
            [inherited.copy()],
        )

        self.assertEqual(failures, [inherited, added])

    def test_inherited_diagnostics_match_all_fields_and_counts(self) -> None:
        inherited = {
            "category": "internal_links",
            "code": "unsupported_scheme",
            "path": "<reference>",
            "detail": "custom:original-fingerprint",
        }
        changed = [
            {**inherited, field: "different"} for field in ("category", "code", "path", "detail")
        ]
        self.assertEqual(
            _new_structural_failures(
                [inherited.copy(), inherited.copy(), *changed], [inherited.copy()]
            ),
            [inherited, *changed],
        )

    def test_preflight_preserves_source_specific_references(self) -> None:
        from trans_novel.assemble import preflight_epub
        from trans_novel.ingest import load_document

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            original = Path(directory) / "original.epub"
            write_phase9_epub(str(original))
            with zipfile.ZipFile(original) as zin, zipfile.ZipFile(source, "w") as zout:
                for info in zin.infolist():
                    data = zin.read(info.filename)
                    if info.filename == "OEBPS/text/chapter-1.xhtml":
                        data = data.replace(
                            b"</body>",
                            b'<a href="custom:embed:0006?mime=image/jpg">Image</a></body>',
                        )
                    zout.writestr(info, data)
            doc = load_document(str(source), "ja", "zh")
            for bilingual in (False, True):
                with self.subTest(bilingual=bilingual):
                    report = preflight_epub(doc, str(source), bilingual=bilingual)
                    self.assertTrue(report["passed"], report["failures"])

    def test_schema4_bilingual_proof_supersedes_legacy_source_heuristics(self) -> None:
        failures = [
            {"code": "source_node_count"},
            {"code": "source_node_misplaced"},
            {"code": "source_node_unexpected"},
            {"code": "source_node_empty"},
        ]

        self.assertEqual(
            _new_structural_failures(
                failures,
                [],
                state_backed_bilingual=True,
            ),
            [{"code": "source_node_empty"}],
        )

    def test_output_paths_ignore_inserted_source_siblings(self) -> None:
        root = etree.fromstring(
            b"<html><body><p id='first'/><p class='tn-source'/><p id='second'/></body></html>"
        )

        self.assertEqual(_resolve_output_path(root, (0, 1)).get("id"), "second")

    def test_generic_source_pairing_includes_preserved_footnote_text(self) -> None:
        source = etree.fromstring(
            b"<html><body><p>Sentence<sup id='note'><a href='#note'>1</a></sup></p></body></html>"
        )
        output = etree.fromstring(
            (
                f"<html><body><p>Translation</p><p class='{BILINGUAL_SOURCE_CLASS}'>"
                "Sentence<sup>1</sup></p></body></html>"
            ).encode()
        )
        segment = SimpleNamespace(
            source="Sentence",
            target="Translation",
            kind="text",
            epub_state=SimpleNamespace(block_path=(0, 0), slots=[]),
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

        self.assertNotIn("source_target_pair_mismatch", {item["code"] for item in failures})

    def test_public_facade_signatures_preserve_annotations(self) -> None:
        validate = get_type_hints(epub_verifier.validate_epub)
        self.assertEqual(
            validate,
            {
                "path": Path,
                "source_path": Path | None,
                "bilingual": bool | None,
                "return": dict[str, Any],
            },
        )
        triplet = get_type_hints(epub_verifier.validate_epub_triplet)
        self.assertEqual(
            triplet,
            {
                "source_path": Path,
                "mono_path": Path,
                "bilingual_path": Path,
                "mono_theme_plan": ThemePlan | None,
                "bilingual_theme_plan": ThemePlan | None,
                "store": Any | None,
                "target_lang": str | None,
                "bilingual_order": str,
                "return": dict[str, Any],
            },
        )
        self.assertEqual(
            list(signature(epub_verifier.validate_epub).parameters),
            ["path", "source_path", "bilingual"],
        )
        self.assertEqual(
            list(signature(epub_verifier.validate_epub_triplet).parameters),
            [
                "source_path",
                "mono_path",
                "bilingual_path",
                "mono_theme_plan",
                "bilingual_theme_plan",
                "store",
                "target_lang",
                "bilingual_order",
            ],
        )

    def test_real_schema4_mono_report_counts_actual_authorized_changes(self) -> None:
        from trans_novel.assemble import assemble

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            store, _ = _run(str(source), str(root / "state"))
            output = root / "mono.epub"
            assemble(store, str(source), str(output), out_format="epub")
            report = store.load_epub_verification()
            assert report is not None
            self.assertTrue(report["passed"])
            self.assertEqual(
                report["authorized_differences"],
                {"text_slots": 18, "toc_labels": 4, "language_fields": 1, "bilingual_nodes": 0},
            )

    def test_nested_nav_label_locator_matches_writer_clear_contract(self) -> None:
        root = etree.fromstring(
            b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            b'<nav epub:type="toc"><ol><li><a href="chapter.xhtml">'
            b"Old <span><em>Nested</em></span></a></li></ol></nav></body></html>"
        )
        locations = nav_label_locations(root, is_ncx=False)
        self.assertEqual(len(locations), 1)
        label, path = locations[0]
        self.assertEqual(label.tag.rsplit("}", 1)[-1], "a")
        self.assertEqual(path, (0, 0, 0, 0, 0))

    def test_source_dom_slot_tag_attr_and_nav_mutations_fail_exactly(self) -> None:
        def mutate(source: Path, output: Path, member: str, transform) -> None:
            with zipfile.ZipFile(source) as zin, zipfile.ZipFile(output, "w") as zout:
                zout.comment = zin.comment
                for info in zin.infolist():
                    data = zin.read(info.filename)
                    if info.filename == member:
                        data = transform(data)
                    zout.writestr(info, data)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            store, _ = _run(str(source), str(root / "state"))
            cases = (
                (
                    "OEBPS/text/chapter-1.xhtml",
                    lambda data: data.replace(b'id="intro"', b'id="changed"'),
                    "unauthorized_dom_change",
                ),
                (
                    "OEBPS/text/chapter-1.xhtml",
                    lambda data: data.replace(b'<p id="intro">', b'<div id="intro">'),
                    "unauthorized_dom_change",
                ),
                (
                    "OEBPS/text/chapter-1.xhtml",
                    lambda data: data.replace("润".encode(), "错误".encode(), 1),
                    "slot_value_mismatch",
                ),
                (
                    "OEBPS/nav.xhtml",
                    lambda data: data.replace(b"Chapter One", b"Wrong label", 1),
                    "label_value_mismatch",
                ),
            )
            for index, (member, transform, expected_code) in enumerate(cases):
                output = root / f"mutated-{index}.epub"
                mutate(source, output, member, transform)
                report = verify_epub(
                    output,
                    source_path=source,
                    store=store,
                    mode="monolingual",
                )
                self.assertFalse(report["passed"], report)
                self.assertIn(expected_code, {item["code"] for item in report["failures"]})

    def test_recovered_schema4_resource_reports_recovered_assurance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recovered.epub"
            chapter = b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>broken"
            container = (
                b"<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                b"<rootfiles><rootfile full-path='content.opf'/></rootfiles></container>"
            )
            opf = (
                b"<package xmlns='http://www.idpf.org/2007/opf'><metadata>"
                b"<dc:language xmlns:dc='http://purl.org/dc/elements/1.1/'>en</dc:language>"
                b"</metadata><manifest><item id='c' href='chapter.xhtml' "
                b"media-type='application/xhtml+xml'/></manifest><spine><itemref idref='c'/>"
                b"</spine></package>"
            )
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)
                archive.writestr("META-INF/container.xml", container)
                archive.writestr("content.opf", opf)
                archive.writestr("chapter.xhtml", chapter)

            from trans_novel.epub.markup import resource_parser

            parser_diagnostics = resource_parser(chapter)[2]

            class Store:
                def load_manifest(self):
                    return {
                        "target_lang": "zh",
                        "chapters": [],
                        "meta": {
                            "epub_schema": 4,
                            "epub_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                            "epub_resources": [
                                {
                                    "href": "chapter.xhtml",
                                    "resource_sha256": hashlib.sha256(chapter).hexdigest(),
                                    "parse_mode": "recovered",
                                    "parser_diagnostics": parser_diagnostics,
                                }
                            ],
                        },
                    }

                def load_chapter(self, index):
                    raise AssertionError(index)

            report = verify_epub(
                source,
                source_path=source,
                store=Store(),
                mode="monolingual",
            )
            self.assertFalse(report["passed"])
            self.assertTrue(
                any(item["code"].startswith("recovered_diagnostic_") for item in report["warnings"])
            )
            mismatch_store = Store()
            original_load_manifest = mismatch_store.load_manifest

            def load_mismatch():
                value = original_load_manifest()
                value["meta"]["epub_resources"][0]["parser_diagnostics"] = []
                return value

            mismatch_store.load_manifest = load_mismatch
            mismatch_report = verify_epub(
                source,
                source_path=source,
                store=mismatch_store,
                mode="monolingual",
            )
            self.assertTrue(
                any(
                    item["code"] == "recovered_diagnostic_mismatch"
                    for item in mismatch_report["failures"]
                )
            )

    def test_ncx_navigation_ignores_comments_and_processing_instructions(self) -> None:
        root = etree.fromstring(
            b"<ncx><!--keep--><?keep pi?><navMap><navPoint><navLabel>"
            b"<text>Chapter</text></navLabel></navPoint></navMap></ncx>"
        )

        labels = nav_label_locations(root, is_ncx=True)

        self.assertEqual([label.text for label, _ in labels], ["Chapter"])

    def test_ncx_root_language_and_comment_pi_contents_are_immutable(self) -> None:
        from trans_novel.assemble.epub.rendering import rewrite_toc_lxml as _rewrite_toc_lxml

        data = (
            b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" xml:lang="ja">'
            b"<navMap><navPoint><navLabel><text>Old<!--keep-->"
            b' One<?keep pi?> tail</text></navLabel><content src="ch.xhtml"/>'
            b"</navPoint></navMap></ncx>"
        )
        result = _rewrite_toc_lxml(
            data,
            [
                {
                    "toc_path": "toc.ncx",
                    "node_index": 0,
                    "raw_href": "ch.xhtml",
                    "title_translated": "New",
                }
            ],
            is_ncx=True,
            toc_path="toc.ncx",
            target_lang="zh-Hans",
        )
        root = etree.fromstring(result)
        self.assertEqual(root.get("{http://www.w3.org/XML/1998/namespace}lang"), "ja")
        label = root.xpath("//*[local-name()='text']")[0]
        self.assertEqual(label.text, "New")
        self.assertEqual(label[0].text, "keep")
        self.assertEqual(label[1].text, "pi")

    def test_toc_label_outer_tail_is_preserved_and_inner_tail_corruption_fails(self) -> None:
        for label_name in ("a", "text"):
            source = etree.fromstring(
                f"<root><{label_name}>Old<!--keep--></{label_name}> OUT</root>".encode()
            )
            output = etree.fromstring(
                f"<root><{label_name}>New<!--keep--></{label_name}> OUT</root>".encode()
            )
            self.assertTrue(
                compare_dom(
                    source,
                    output,
                    {((0,), "text"): {"kind": "toc", "expected": "New", "count": True}},
                    toc_label_paths={(0,)},
                )
            )
            output[0][0].tail = " stale"
            self.assertFalse(
                compare_dom(
                    source,
                    output,
                    {((0,), "text"): {"kind": "toc", "expected": "New", "count": True}},
                    toc_label_paths={(0,)},
                )
            )


class TestEpubReadability(unittest.TestCase):
    def test_output_reopen_failures_are_not_discarded(self) -> None:
        for code in ("reopen_empty", "reopen_failed", None):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.epub"
                output = Path(directory) / "output.tmp"
                write_phase9_epub(str(source))
                with zipfile.ZipFile(source) as zin, zipfile.ZipFile(output, "w") as zout:
                    for info in zin.infolist():
                        data = zin.read(info.filename)
                        if code == "reopen_failed" and info.filename == "OEBPS/content.opf":
                            data = data.replace(b"application/xhtml+xml", b"image/png")
                        elif code == "reopen_empty" and info.filename.startswith("OEBPS/text/"):
                            root = etree.fromstring(data)
                            body = root.find("{http://www.w3.org/1999/xhtml}body")
                            assert body is not None
                            for child in list(body):
                                if etree.QName(child).localname != "h1":
                                    body.remove(child)
                                else:
                                    child.text = None
                            etree.SubElement(
                                body,
                                "{http://www.w3.org/1999/xhtml}img",
                                src="../images/figure.png",
                            )
                            data = etree.tostring(root)
                        zout.writestr(info, data)
                report = verify_epub(output)
                if code is None:
                    self.assertTrue(report["passed"], report["failures"])
                else:
                    self.assertFalse(report["passed"])
                    self.assertIn(code, {item["code"] for item in report["failures"]})
