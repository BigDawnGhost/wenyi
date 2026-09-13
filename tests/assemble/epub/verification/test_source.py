from __future__ import annotations

import unittest
from types import SimpleNamespace

from lxml import etree

from trans_novel.assemble.epub.rendering import (
    BILINGUAL_DIRECT_TARGET_CLASS,
    BILINGUAL_SOURCE_CLASS,
)
from trans_novel.assemble.epub.rendering import (
    add_bilingual_sources as _add_bilingual_sources,
)
from trans_novel.assemble.epub.verification.bilingual import bilingual_proof


class TestDirectRunPairing(unittest.TestCase):
    def test_direct_br_pairing_order_is_checked_for_both_orders(self) -> None:
        source = etree.fromstring(b"<html><head></head><body><p>One<br/>Two</p></body></html>")
        state_one = SimpleNamespace(
            block_path=(1, 0),
            slots=[
                SimpleNamespace(
                    element_path=(),
                    field="text",
                    source_value="One",
                    target_value="Uno",
                )
            ],
        )
        state_two = SimpleNamespace(
            block_path=(1, 0),
            slots=[
                SimpleNamespace(
                    element_path=(0,),
                    field="tail",
                    source_value="Two",
                    target_value="Dos",
                )
            ],
        )
        segments = [
            SimpleNamespace(kind="text", source="One", target="Uno", epub_state=state_one),
            SimpleNamespace(kind="text", source="Two", target="Dos", epub_state=state_two),
        ]

        def check(
            order: str,
            invert_first_pair: bool,
            source_root: etree._Element = source,
            block_tag: str = "p",
        ) -> list[dict[str, str]]:
            target_first = (
                '<span class="tn-bilingual-target">Uno</span><span class="tn-source '
                'ibooks-dark-theme-use-custom-text-color">One</span>'
            )
            source_first = (
                '<span class="tn-source ibooks-dark-theme-use-custom-text-color">'
                'One</span><span class="tn-bilingual-target">Uno</span>'
            )
            second = (
                '<span class="tn-bilingual-target">Dos</span><span class="tn-source '
                'ibooks-dark-theme-use-custom-text-color">Two</span>'
                if order == "target_first"
                else '<span class="tn-source ibooks-dark-theme-use-custom-text-color">'
                'Two</span><span class="tn-bilingual-target">Dos</span>'
            )
            first = (
                source_first if (order == "target_first" and invert_first_pair) else target_first
            )
            if order == "source_first":
                first = target_first if invert_first_pair else source_first
            markup = (
                f"<html><head></head><body><{block_tag}>{first}<br/>{second}</{block_tag}></body></html>"
            ).encode()
            failures: list[dict[str, str]] = []
            bilingual_proof(
                source_root,
                etree.fromstring(markup),
                segments,
                source_lang="en",
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            return failures

        for order in ("source_first", "target_first"):
            self.assertNotIn("source_node_order", {item["code"] for item in check(order, False)})
            self.assertIn("source_node_order", {item["code"] for item in check(order, True)})
        div_source = etree.fromstring(
            b"<html><head></head><body><div>One<br/>Two</div></body></html>"
        )
        for order in ("source_first", "target_first"):
            self.assertNotIn(
                "source_node_order",
                {item["code"] for item in check(order, False, div_source, "div")},
            )
            self.assertIn(
                "source_node_order",
                {item["code"] for item in check(order, True, div_source, "div")},
            )

    def test_direct_br_nested_inline_slots_pair_at_each_actual_owner(self) -> None:
        source = etree.fromstring(
            b"<html><head></head><body><p>One <em>two</em> tail<br/>Next</p></body></html>"
        )
        slots = [
            SimpleNamespace(
                element_path=(),
                field="text",
                source_value="One ",
                target_value="Uno",
            ),
            SimpleNamespace(
                element_path=(0,),
                field="text",
                source_value="two",
                target_value="dos",
            ),
            SimpleNamespace(
                element_path=(0,),
                field="tail",
                source_value=" tail",
                target_value="cola",
            ),
            SimpleNamespace(
                element_path=(1,),
                field="tail",
                source_value="Next",
                target_value="Siguiente",
            ),
        ]
        state = SimpleNamespace(block_path=(1, 0), slots=slots)
        segment = SimpleNamespace(
            kind="text",
            source="One two tail Next",
            target="Uno dos cola Siguiente",
            epub_state=state,
        )
        for order in ("target_first", "source_first"):
            output = etree.fromstring(etree.tostring(source))
            output_block = output.xpath(".//p")[0]
            self.assertEqual(
                _add_bilingual_sources(
                    output,
                    [segment],
                    order=order,
                    source_blocks={(1, 0): etree.fromstring(etree.tostring(output_block))},
                    block_refs={(1, 0): output_block},
                ),
                4,
            )
            self.assertEqual(len(output.xpath(".//p//br")), 1)
            self.assertEqual(len(output.xpath(".//p//span[contains(@class, 'tn-source')]")), 4)
            failures: list[dict[str, str]] = []
            count = bilingual_proof(
                source,
                output,
                [segment],
                source_lang="en",
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertEqual(count, 4)
            self.assertEqual(failures, [])
        corrupt = etree.fromstring(etree.tostring(source))
        corrupt_block = corrupt.xpath(".//p")[0]
        _add_bilingual_sources(
            corrupt,
            [segment],
            order="target_first",
            source_blocks={(1, 0): etree.fromstring(etree.tostring(corrupt_block))},
            block_refs={(1, 0): corrupt_block},
        )
        first_target, first_source = corrupt_block[0], corrupt_block[1]
        corrupt_block.remove(first_target)
        corrupt_block.remove(first_source)
        corrupt_block.insert(0, first_source)
        corrupt_block.insert(1, first_target)
        corrupt_failures: list[dict[str, str]] = []
        bilingual_proof(
            source,
            corrupt,
            [segment],
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=corrupt_failures,
        )
        self.assertIn("source_node_order", {item["code"] for item in corrupt_failures})

    def test_nested_bilingual_order_is_checked_in_parent_text(self) -> None:
        source = etree.fromstring(b"<html><body><li>Original</li></body></html>")
        state = SimpleNamespace(block_path=(0, 0))
        segment = SimpleNamespace(
            kind="text",
            source="Original",
            target="Translated",
            epub_state=state,
        )
        output = etree.fromstring(
            b'<html><body><li><div class="tn-source '
            b'ibooks-dark-theme-use-custom-text-color">Original</div>Translated</li></body></html>'
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
        self.assertIn("source_node_order", {item["code"] for item in failures})

    def test_container_mixed_content_accepts_both_orders_and_rejects_inversion(self) -> None:
        source = etree.fromstring(
            b"<html><head></head><body><li><em>Original</em> tail</li></body></html>"
        )
        segment = SimpleNamespace(
            kind="text",
            source="Original tail",
            target="Translated tail",
            epub_state=SimpleNamespace(block_path=(1, 0)),
        )
        source_markup = (
            '<div class="tn-source ibooks-dark-theme-use-custom-text-color">'
            "<em>Original</em> tail</div>"
        )
        for order, body in (
            (
                "target_first",
                f"<li><em>Translated</em> tail{source_markup}</li>",
            ),
            (
                "source_first",
                f"<li>{source_markup}<em>Translated</em> tail</li>",
            ),
        ):
            output = etree.fromstring(f"<html><head></head><body>{body}</body></html>".encode())
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

        inverted = etree.fromstring(
            f"<html><head></head><body><li>{source_markup}<em>Translated</em> tail</li></body></html>".encode()
        )
        failures = []
        bilingual_proof(
            source,
            inverted,
            [segment],
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=failures,
        )
        self.assertIn("source_node_order", {item["code"] for item in failures})

    def test_container_direct_br_uses_one_nested_source_div_in_both_orders(self) -> None:
        source = etree.fromstring(b"<html><head></head><body><li>One<br/>Two</li></body></html>")
        block = source.xpath(".//li")[0]
        state = SimpleNamespace(block_path=(1, 0), slots=[])
        segments = [
            SimpleNamespace(kind="text", source="One", target="Uno", epub_state=state),
            SimpleNamespace(kind="text", source="Two", target="Dos", epub_state=state),
        ]
        for order in ("target_first", "source_first"):
            output = etree.fromstring(etree.tostring(source))
            output_block = output.xpath(".//li")[0]
            added = _add_bilingual_sources(
                output,
                segments,
                order=order,
                source_blocks={(1, 0): etree.fromstring(etree.tostring(block))},
                block_refs={(1, 0): output_block},
            )
            self.assertEqual(added, 1)
            source_nodes = output.xpath(".//*[contains(@class, 'tn-source')]")
            self.assertEqual(len(source_nodes), 1)
            self.assertEqual(source_nodes[0].tag.rsplit("}", 1)[-1], "div")
            failures: list[dict[str, str]] = []
            bilingual_proof(
                source,
                output,
                segments,
                source_lang="en",
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertNotIn("source_node_order", {item["code"] for item in failures})
            if order == "target_first":
                corrupt = etree.fromstring(etree.tostring(source))
                corrupt_block = corrupt.xpath(".//li")[0]
                _add_bilingual_sources(
                    corrupt,
                    segments,
                    order=order,
                    source_blocks={(1, 0): etree.fromstring(etree.tostring(block))},
                    block_refs={(1, 0): corrupt_block},
                )
                corrupt_source = corrupt_block[-1]
                corrupt_block.remove(corrupt_source)
                corrupt_block.insert(0, corrupt_source)
                corrupt_failures: list[dict[str, str]] = []
                bilingual_proof(
                    source,
                    corrupt,
                    segments,
                    source_lang="en",
                    order=order,
                    resource="chapter.xhtml",
                    failures=corrupt_failures,
                )
                self.assertIn("source_node_order", {item["code"] for item in corrupt_failures})


class TestDirectRunSlots(unittest.TestCase):
    def test_direct_br_nested_inline_slots_pair_at_each_actual_owner(self) -> None:
        source = etree.fromstring(
            b"<html><head></head><body><p>One <em>two</em> tail<br/>Next</p></body></html>"
        )
        slots = [
            SimpleNamespace(
                element_path=(),
                field="text",
                source_value="One ",
                target_value="Uno",
            ),
            SimpleNamespace(
                element_path=(0,),
                field="text",
                source_value="two",
                target_value="dos",
            ),
            SimpleNamespace(
                element_path=(0,),
                field="tail",
                source_value=" tail",
                target_value="cola",
            ),
            SimpleNamespace(
                element_path=(1,),
                field="tail",
                source_value="Next",
                target_value="Siguiente",
            ),
        ]
        state = SimpleNamespace(block_path=(1, 0), slots=slots)
        segment = SimpleNamespace(
            kind="text",
            source="One two tail Next",
            target="Uno dos cola Siguiente",
            epub_state=state,
        )
        for order in ("target_first", "source_first"):
            output = etree.fromstring(etree.tostring(source))
            output_block = output.xpath(".//p")[0]
            self.assertEqual(
                _add_bilingual_sources(
                    output,
                    [segment],
                    order=order,
                    source_blocks={(1, 0): etree.fromstring(etree.tostring(output_block))},
                    block_refs={(1, 0): output_block},
                ),
                4,
            )
            self.assertEqual(len(output.xpath(".//p//br")), 1)
            self.assertEqual(len(output.xpath(".//p//span[contains(@class, 'tn-source')]")), 4)
            failures: list[dict[str, str]] = []
            count = bilingual_proof(
                source,
                output,
                [segment],
                source_lang="en",
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertEqual(count, 4)
            self.assertEqual(failures, [])
        corrupt = etree.fromstring(etree.tostring(source))
        corrupt_block = corrupt.xpath(".//p")[0]
        _add_bilingual_sources(
            corrupt,
            [segment],
            order="target_first",
            source_blocks={(1, 0): etree.fromstring(etree.tostring(corrupt_block))},
            block_refs={(1, 0): corrupt_block},
        )
        first_target, first_source = corrupt_block[0], corrupt_block[1]
        corrupt_block.remove(first_target)
        corrupt_block.remove(first_source)
        corrupt_block.insert(0, first_source)
        corrupt_block.insert(1, first_target)
        corrupt_failures: list[dict[str, str]] = []
        bilingual_proof(
            source,
            corrupt,
            [segment],
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=corrupt_failures,
        )
        self.assertIn("source_node_order", {item["code"] for item in corrupt_failures})

    def test_direct_br_whitespace_source_is_coalesced_and_paired(self) -> None:
        source = etree.fromstring(
            b"<html><head></head><body><p>One<em>two</em> "
            b"<span>Next</span><br/>After</p></body></html>"
        )
        slots = [
            SimpleNamespace(element_path=(), field="text", source_value="One", target_value="Uno"),
            SimpleNamespace(
                element_path=(0,), field="text", source_value="two", target_value="dos"
            ),
            SimpleNamespace(element_path=(0,), field="tail", source_value=" ", target_value=""),
            SimpleNamespace(
                element_path=(1,), field="text", source_value="Next", target_value="Siguiente"
            ),
        ]
        segment = SimpleNamespace(
            kind="text",
            source="One two Next",
            target="Uno dos Siguiente",
            epub_state=SimpleNamespace(block_path=(1, 0), slots=slots),
        )
        for order in ("target_first", "source_first"):
            output = etree.fromstring(etree.tostring(source))
            output_block = output.xpath(".//p")[0]
            added = _add_bilingual_sources(
                output,
                [segment],
                order=order,
                source_blocks={(1, 0): etree.fromstring(etree.tostring(output_block))},
                block_refs={(1, 0): output_block},
            )
            self.assertEqual(added, 3)
            self.assertEqual(
                [node.text for node in output.xpath(".//p//span[contains(@class, 'tn-source')]")],
                ["One", "two ", "Next"],
            )
            self.assertEqual(
                len(output.xpath(".//p//span[@class='tn-bilingual-target']")),
                3,
            )
            failures: list[dict[str, str]] = []
            count = bilingual_proof(
                source,
                output,
                [segment],
                source_lang="en",
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertEqual(count, 3)
            self.assertNotIn("source_node_empty", {item["code"] for item in failures})

    def test_direct_run_active_link_and_original_span_both_orders(self) -> None:
        source = etree.fromstring(
            b"<html><head></head><body><p><a href='next.xhtml'>One</a>"
            b"<span>keep</span><br/>Two</p></body></html>"
        )
        block = source.xpath(".//p")[0]
        slots = [
            SimpleNamespace(
                element_path=(0,),
                field="text",
                source_value="One",
                target_value="Uno",
            ),
            SimpleNamespace(
                element_path=(2,),
                field="tail",
                source_value="Two",
                target_value="Dos",
            ),
        ]
        segment = SimpleNamespace(
            kind="text",
            source="One Two",
            target="Uno Dos",
            epub_state=SimpleNamespace(block_path=(1, 0), slots=slots),
        )
        for order in ("target_first", "source_first"):
            output = etree.fromstring(etree.tostring(source))
            output_block = output.xpath(".//p")[0]
            self.assertEqual(
                _add_bilingual_sources(
                    output,
                    [segment],
                    order=order,
                    source_blocks={(1, 0): etree.fromstring(etree.tostring(block))},
                    block_refs={(1, 0): output_block},
                ),
                2,
            )
            original_span = next(
                node for node in output_block.xpath("./span") if node.text == "keep"
            )
            self.assertEqual(dict(original_span.attrib), {})
            self.assertEqual(original_span.text, "keep")
            for node in output.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' tn-source ')]"
            ):
                self.assertFalse(
                    any(
                        ancestor.tag.rsplit("}", 1)[-1] in {"a", "ruby"}
                        for ancestor in node.iterancestors()
                    )
                )
            self.assertEqual(
                len(output.xpath(f".//span[@class='{BILINGUAL_DIRECT_TARGET_CLASS}']")),
                2,
            )
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


class TestDirectRunActiveButton(unittest.TestCase):
    def _button_cases(self):
        return (
            (
                "<html><head></head><body><p><button onclick='go()'>One</button>"
                "<br/>Two</p></body></html>",
                "en",
                [
                    SimpleNamespace(
                        element_path=(0,), field="text", source_value="One", target_value="Uno"
                    ),
                    SimpleNamespace(
                        element_path=(1,), field="tail", source_value="Two", target_value="Dos"
                    ),
                ],
            ),
            (
                "<html><head></head><body><p><span onclick='go()'>One</span>"
                "<br/>Two</p></body></html>",
                "en",
                [
                    SimpleNamespace(
                        element_path=(0,), field="text", source_value="One", target_value="Uno"
                    ),
                    SimpleNamespace(
                        element_path=(1,), field="tail", source_value="Two", target_value="Dos"
                    ),
                ],
            ),
            (
                "<html><head></head><body><p><ruby>漢<rt>かん</rt></ruby>"
                " tail<br/>Next</p></body></html>",
                "ja",
                [
                    SimpleNamespace(
                        element_path=(0,), field="tail", source_value=" tail", target_value=" 尾"
                    ),
                    SimpleNamespace(
                        element_path=(1,), field="tail", source_value="Next", target_value="下"
                    ),
                ],
            ),
        )

    def _check_button_case(self, markup, source_lang, slots):
        source = etree.fromstring(markup.encode())
        segment = SimpleNamespace(
            kind="text",
            source="source",
            target="target",
            epub_state=SimpleNamespace(block_path=(1, 0), slots=slots),
        )
        for order in ("target_first", "source_first"):
            output = etree.fromstring(markup.encode())
            block = output.xpath(".//p")[0]
            _add_bilingual_sources(
                output,
                [segment],
                order=order,
                source_blocks={(1, 0): etree.fromstring(markup.encode()).xpath(".//p")[0]},
                block_refs={(1, 0): block},
            )
            source_nodes = output.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' tn-source ')]"
            )
            self.assertEqual(len(source_nodes), len(slots))
            for node in source_nodes:
                self.assertFalse(
                    any(
                        ancestor.tag.rsplit("}", 1)[-1] in {"a", "ruby", "button"}
                        or any(key.lower().startswith("on") for key in ancestor.attrib)
                        for ancestor in node.iterancestors()
                    )
                )
            if source_lang == "ja":
                source_tail = next(node for node in source_nodes if node.text == " tail")
                target_tail = next(
                    node
                    for node in output.xpath(".//span")
                    if node.get("class") == BILINGUAL_DIRECT_TARGET_CLASS and node.text == " 尾"
                )
                ruby = output.xpath(".//ruby")[0]
                if order == "target_first":
                    self.assertGreater(block.index(source_tail), block.index(target_tail))
                    self.assertGreater(block.index(source_tail), block.index(ruby))
                else:
                    self.assertLess(block.index(source_tail), block.index(ruby))
                    self.assertLess(block.index(source_tail), block.index(target_tail))
                self.assertFalse(source_tail.xpath(".//*[local-name()='ruby']"))
                corrupt = etree.fromstring(etree.tostring(output))
                corrupt_block = corrupt.xpath(".//p")[0]
                corrupt_source = next(
                    node
                    for node in corrupt_block
                    if node.get("class") == BILINGUAL_SOURCE_CLASS and node.text == " tail"
                )
                corrupt_target = next(
                    node
                    for node in corrupt_block
                    if node.get("class") == BILINGUAL_DIRECT_TARGET_CLASS and node.text == " 尾"
                )
                corrupt_block.remove(corrupt_source)
                corrupt_block.remove(corrupt_target)
                ruby_index = corrupt_block.index(corrupt_block.xpath("./ruby")[0])
                corrupt_block.insert(
                    ruby_index + 1,
                    corrupt_source if order == "target_first" else corrupt_target,
                )
                corrupt_block.insert(
                    ruby_index + 2,
                    corrupt_target if order == "target_first" else corrupt_source,
                )
                corrupt_failures: list[dict[str, str]] = []
                bilingual_proof(
                    source,
                    corrupt,
                    [segment],
                    source_lang=source_lang,
                    order=order,
                    resource="chapter.xhtml",
                    failures=corrupt_failures,
                )
                self.assertIn("source_node_order", {item["code"] for item in corrupt_failures})
            failures: list[dict[str, str]] = []
            bilingual_proof(
                source,
                output,
                [segment],
                source_lang=source_lang,
                order=order,
                resource="chapter.xhtml",
                failures=failures,
            )
            self.assertNotIn("source_node_active_ancestor", {item["code"] for item in failures})
            self.assertNotIn("source_target_pair_mismatch", {item["code"] for item in failures})
        return markup, segment

    def _check_button_reparenting(self, markup, segment):
        corrupt = etree.fromstring(markup.encode())
        corrupt_block = corrupt.xpath(".//p")[0]
        _add_bilingual_sources(
            corrupt,
            [segment],
            order="target_first",
            source_blocks={(1, 0): etree.fromstring(markup.encode()).xpath(".//p")[0]},
            block_refs={(1, 0): corrupt_block},
        )
        source_node = corrupt.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' tn-source ')]"
        )[0]
        corrupt_block.remove(source_node)
        corrupt.xpath(".//button")[0].append(source_node)
        failures = []
        bilingual_proof(
            etree.fromstring(markup.encode()),
            corrupt,
            [segment],
            source_lang="en",
            order="target_first",
            resource="chapter.xhtml",
            failures=failures,
        )
        self.assertTrue(failures)

    def test_direct_run_active_button_onclick_and_ruby_tail_both_orders(self) -> None:
        for markup, source_lang, slots in self._button_cases():
            markup, segment = self._check_button_case(markup, source_lang, slots)
            if "button" in markup:
                self._check_button_reparenting(markup, segment)
