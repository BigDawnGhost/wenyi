from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from trans_novel.agents.layout_analyzer import LayoutObservation
from trans_novel.assemble.epub.layout import build_layout_inventory
from trans_novel.ingest import Chapter, Segment
from trans_novel.pipeline.nodes.layout_analysis import analyze_layout

_CONTAINER = b"""<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="O/content.opf"/></rootfiles></container>"""
_OPF = b"""<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/><item id="cover" href="cover.xhtml" media-type="application/xhtml+xml" properties="cover-image"/><item id="fixed" href="fixed.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="cover"/><itemref idref="fixed" properties="rendition:layout-pre-paginated"/><itemref idref="c"/></spine></package>"""
_PROTECTED = b"""<html xmlns="http://www.w3.org/1999/xhtml"><head><style>p[ {color:red}</style></head><body><p>PROTECTED_RESOURCE</p></body></html>"""


def _write_epub(path: Path, quoted: bytes = b"Quoted") -> None:
    chapter = b"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><style>.block{font-style:italic}.quote{margin-left:2em}</style></head><body><div><p class="block">QUOTED</p><p class="block">Summary</p></div><p class="quote">Styled</p><aside id="n" epub:type="footnote"><p class="footnote1">A note</p></aside><section epub:type="bibliography"><p class="footnote1">A book</p></section><a epub:type="noteref" href="#n">1</a><script>secret</script></body></html>""".replace(
        b"QUOTED", quoted
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr("O/content.opf", _OPF)
        archive.writestr("O/c.xhtml", chapter)
        archive.writestr("O/cover.xhtml", _PROTECTED)
        archive.writestr("O/fixed.xhtml", _PROTECTED)


def _write_case_epub(path: Path) -> None:
    paragraphs = b"".join(
        f'<p class="{"Quote" if index == 3 else "quote"}">{index}</p>'.encode()
        for index in range(9)
    )
    chapter = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>' + paragraphs + b"</body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr("O/content.opf", _OPF)
        archive.writestr("O/c.xhtml", chapter)


def _write_nth_child_epub(path: Path) -> None:
    paragraphs = b"".join(f'<p class="quote">{index}</p>'.encode() for index in range(9))
    chapter = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        b"<style>p:nth-child(4){font-style:italic}</style></head><body>"
        + paragraphs
        + b"</body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr("O/content.opf", _OPF)
        archive.writestr("O/c.xhtml", chapter)


class TestLayoutInventory(unittest.TestCase):
    def test_generated_inventory_uses_merged_original_paragraphs_and_ignores_targets(self):
        chapter = Chapter(
            index=3,
            segments=[
                Segment(index=0, source="First ", target="译一"),
                Segment(index=1, source="continued", target="译二", cont=True),
                Segment(index=2, source="Heading", target="标题", kind="heading"),
            ],
        )
        first = build_layout_inventory("book.txt", [chapter])
        changed = chapter.model_copy(deep=True)
        changed.segments[0].target = "完全不同"
        second = build_layout_inventory("book.txt", [changed])

        self.assertEqual(first, second)
        self.assertEqual([node.resource_href for node in first.nodes], ["ch3.xhtml", "ch3.xhtml"])
        self.assertEqual([node.path for node in first.nodes], [(0,), (1,)])
        self.assertIn("First continued", first.nodes[0].sample["source_markup"])
        self.assertNotIn("译", str(first.nodes[0].sample))

    def test_epub_inventory_keeps_same_class_instances_notes_references_css_and_source_identity(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.epub"
            _write_epub(source)
            inventory = build_layout_inventory(str(source), [])
            _write_epub(source, b"Changed")
            changed = build_layout_inventory(str(source), [])

        block_nodes = [
            node for node in inventory.nodes if "block" in node.sample["features"]["classes"]
        ]
        self.assertEqual(len(block_nodes), 2)
        self.assertEqual(block_nodes[0].group_key, block_nodes[1].group_key)
        styled = next(
            node for node in inventory.nodes if "quote" in node.sample["features"]["classes"]
        )
        self.assertIn(".quote", "".join(styled.sample["matched_css"]))
        evidence = str([node.sample for node in inventory.nodes])
        self.assertIn("footnote", evidence)
        self.assertIn("bibliography", evidence)
        self.assertIn("noteref", evidence)
        footnote_nodes = [
            node for node in inventory.nodes if "footnote1" in node.sample["features"]["classes"]
        ]
        ancestor_types = [
            {
                epub_type
                for ancestor in node.sample["ancestors"]
                for epub_type in ancestor["epub_types"]
            }
            for node in footnote_nodes
        ]
        self.assertIn("footnote", ancestor_types[0])
        self.assertIn("bibliography", ancestor_types[1])
        self.assertTrue(footnote_nodes[0].sample["references"]["referenced_by"])
        self.assertNotIn("secret", evidence)
        self.assertNotIn("PROTECTED_RESOURCE", evidence)
        self.assertNotEqual(inventory.source_sha256, changed.source_sha256)
        self.assertNotEqual(inventory.digest, changed.digest)

    def test_epub_node_evidence_and_fingerprint_include_inline_child_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.epub"
            _write_epub(source, b"Quote <em>x</em> tail")
            first = build_layout_inventory(str(source), [])
            _write_epub(source, b"Quote <em>x</em> changed")
            second = build_layout_inventory(str(source), [])

        first_node = next(node for node in first.nodes if "Quote" in node.sample["source_markup"])
        second_node = next(node for node in second.nodes if "Quote" in node.sample["source_markup"])
        self.assertIn(" tail", first_node.sample["source_markup"])
        self.assertNotEqual(first_node.source_sha256, second_node.source_sha256)

    def test_class_tokens_keep_case_so_an_unsampled_outlier_stays_in_its_own_group(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.epub"
            _write_case_epub(source)
            inventory = build_layout_inventory(str(source), [])

        class Analyzer:
            @staticmethod
            def classify(*, samples, allowed_roles):
                return [
                    LayoutObservation(
                        node_id=sample["node_id"],
                        role=(
                            "quote-attribution"
                            if sample["features"]["classes"] == ["Quote"]
                            else "body"
                        ),
                        level=None,
                    )
                    for sample in samples
                ]

        profile = analyze_layout(
            inventory,
            Analyzer(),
            checkpoint=None,
            save_checkpoint=lambda value: None,
        )
        self.assertEqual(inventory.nodes[3].sample["features"]["classes"], ["Quote"])
        self.assertNotEqual(inventory.nodes[3].group_key, inventory.nodes[2].group_key)
        self.assertEqual(profile.assignments[3].role, "quote-attribution")

    def test_matched_source_css_isolates_an_unsampled_same_class_outlier(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.epub"
            _write_nth_child_epub(source)
            inventory = build_layout_inventory(str(source), [])

        class Analyzer:
            @staticmethod
            def classify(*, samples, allowed_roles):
                return [
                    LayoutObservation(
                        node_id=sample["node_id"],
                        role=(
                            "quote"
                            if "font-style:italic" in "".join(sample["matched_css"])
                            else "body"
                        ),
                        level=None,
                    )
                    for sample in samples
                ]

        profile = analyze_layout(
            inventory,
            Analyzer(),
            checkpoint=None,
            save_checkpoint=lambda value: None,
        )
        self.assertEqual(
            {node.sample["features"]["classes"][0] for node in inventory.nodes},
            {"quote"},
        )
        self.assertNotEqual(inventory.nodes[3].group_key, inventory.nodes[2].group_key)
        self.assertEqual(profile.assignments[3].role, "quote")


if __name__ == "__main__":
    unittest.main()
