from __future__ import annotations

import unittest

from lxml import etree

from trans_novel.assemble.epub.rendering.source_dom import resolve_element_path
from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError
from trans_novel.assemble.epub.rendering.theme.projection import build_projection

_XHTML = "http://www.w3.org/1999/xhtml"


def _xml(markup: str) -> etree._Element:
    return etree.fromstring(markup.encode())


class TestThemeProjection(unittest.TestCase):
    def test_source_order_and_direct_wrapper_do_not_change_snapshot(self) -> None:
        mono = _xml(f'<html xmlns="{_XHTML}"><body><p>目标</p></body></html>')
        source_after = _xml(
            f'<html xmlns="{_XHTML}"><body>'
            '<div class="tn-bilingual-target"><p>目标</p></div>'
            '<p class="tn-source ibooks-dark-theme-use-custom-text-color">Source</p>'
            "</body></html>"
        )
        source_before = _xml(
            f'<html xmlns="{_XHTML}"><body>'
            '<p class="tn-source ibooks-dark-theme-use-custom-text-color">Source</p>'
            '<div class="tn-bilingual-target"><p>目标</p></div>'
            "</body></html>"
        )

        expected = build_projection(mono).snapshot
        self.assertEqual(build_projection(source_after).snapshot, expected)
        self.assertEqual(build_projection(source_before).snapshot, expected)

    def test_text_is_bounded_by_unicode_code_points_and_preserves_nbsp(self) -> None:
        text = "你" * 2_049 + "\u00a0 \nZ"
        root = _xml(f'<body xmlns="{_XHTML}"><p>{text}</p></body>')
        paragraph = build_projection(root).snapshot["nodes"][1]

        self.assertEqual(paragraph["text"], "你" * 2_048)
        self.assertEqual(paragraph["textLength"], 2_052)
        self.assertTrue(paragraph["textTruncated"])

        short = _xml(f'<body xmlns="{_XHTML}"><p>A\u00a0  B</p></body>')
        self.assertEqual(build_projection(short).snapshot["nodes"][1]["text"], "A\u00a0 B")

    def test_protection_sources_and_original_context_are_independent(self) -> None:
        root = _xml(
            f'<html xmlns="{_XHTML}" xml:lang="zh-Hans" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<nav><p>目录</p></nav><aside epub:type="footnote"><p>注</p></aside>'
            '<div role="table"><blockquote><ol><li><p aria-level="3" role="heading note">正文</p>'
            "<code>代码</code></li></ol></blockquote></div>"
            '<p class="tn-source ibooks-dark-theme-use-custom-text-color">Source</p>'
            "</body></html>"
        )
        projection = build_projection(root)
        names = [node["tag"] for node in projection.snapshot["nodes"]]
        paragraph = next(
            node for node in projection.snapshot["nodes"] if node["ariaRole"] == "heading"
        )

        self.assertNotIn("nav", names)
        self.assertIn("aside", names)
        self.assertNotIn("code", names)
        self.assertEqual(paragraph["language"], "zh-Hans")
        self.assertEqual(paragraph["ariaRole"], "heading")
        self.assertEqual(paragraph["ariaLevel"], 3)
        self.assertEqual(
            paragraph["context"],
            {"inTable": True, "inNavigation": False, "inQuote": True, "inList": True},
        )
        source = root.xpath('//*[contains(concat(" ", @class, " "), " tn-source ")]')[0]
        self.assertIn(source, projection.source_nodes)
        self.assertNotIn(source, projection.protected)

    def test_explicit_notes_are_eligible_without_relaxing_other_protection(self) -> None:
        root = _xml(
            '<body xmlns:epub="http://www.idpf.org/2007/ops"><p>Main text</p>'
            '<section role="doc-endnotes"><p>Note</p></section>'
            '<figcaption>Figure</figcaption><div epub:type="subtitle">Subtitle</div>'
            '<span role="doc-subtitle">ARIA subtitle</span></body>'
        )
        projection = build_projection(root)
        note_section = root[1]
        note_paragraph = note_section[0]

        self.assertNotIn(note_section, projection.protected)
        self.assertNotIn(note_paragraph, projection.protected)
        record = next(
            item
            for item, node in zip(projection.snapshot["nodes"], projection.nodes, strict=True)
            if node is note_paragraph
        )
        self.assertTrue(record["isTextBlock"])
        eligible = {
            node.tag.rsplit("}", 1)[-1]
            for item, node in zip(projection.snapshot["nodes"], projection.nodes, strict=True)
            if item["isTextBlock"]
        }
        self.assertTrue({"p", "figcaption", "div", "span"}.issubset(eligible))

    def test_explicit_exclusion_and_resource_skip_protect_descendants(self) -> None:
        root = _xml(f'<body xmlns="{_XHTML}"><section><p>x</p></section><p>y</p></body>')
        section = root[0]
        projection = build_projection(root, excluded=(section,))

        self.assertEqual([node["text"] for node in projection.snapshot["nodes"]], ["y", "y"])
        self.assertTrue(set(section.iter()).issubset(projection.protected))
        skipped = build_projection(root, skip_resource=True)
        self.assertEqual(skipped.snapshot["nodes"], [])
        self.assertIn(root, skipped.protected)

    def test_paths_use_existing_element_only_indices_and_keep_wrapper(self) -> None:
        root = _xml(
            f'<html xmlns="{_XHTML}"><!--before--><body><!--inside-->'
            '<div class="tn-bilingual-target"><!--wrapped--><p>x</p></div>'
            "<?pi ignored?><p>y</p></body></html>"
        )
        projection = build_projection(root)

        self.assertEqual(projection.paths, ((0,), (0, 0, 0), (0, 1)))
        for path, node in zip(projection.paths, projection.nodes, strict=True):
            self.assertIs(resolve_element_path(root, path), node)
        first, second = projection.snapshot["nodes"][1:]
        self.assertEqual(first["parentId"], 0)
        self.assertEqual(first["nextSiblingId"], 2)
        self.assertEqual(second["previousSiblingId"], 1)

    def test_unknown_namespaces_and_div_block_candidates_are_projected(self) -> None:
        root = _xml(
            f'<body xmlns="{_XHTML}" xmlns:x="urn:unknown">'
            "<div>outer<div><x:mark>inner</x:mark></div></div></body>"
        )
        nodes = build_projection(root).snapshot["nodes"]

        self.assertFalse(nodes[1]["isTextBlock"])
        self.assertTrue(nodes[2]["isTextBlock"])
        self.assertEqual(nodes[3]["namespace"], "urn:unknown")

    def test_invalid_body_shape_fails_stably(self) -> None:
        for markup in (
            f'<html xmlns="{_XHTML}"><head/></html>',
            f'<html xmlns="{_XHTML}"><body/><body/></html>',
        ):
            with self.subTest(markup=markup), self.assertRaises(ThemeError) as failure:
                build_projection(_xml(markup))
            self.assertEqual(
                (failure.exception.code, str(failure.exception)), ("theme_config", "invalid_body")
            )

    def test_node_limit_is_all_or_nothing(self) -> None:
        root = etree.Element(f"{{{_XHTML}}}body")
        for _ in range(50_000):
            etree.SubElement(root, f"{{{_XHTML}}}span")
        with self.assertRaises(ThemeError) as failure:
            build_projection(root)
        self.assertEqual(
            (failure.exception.code, str(failure.exception)), ("theme_limit", "snapshot_nodes")
        )


if __name__ == "__main__":
    unittest.main()
