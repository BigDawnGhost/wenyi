from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

from trans_novel.assemble.epub.rendering.source_archive import assemble_source_epub
from trans_novel.assemble.epub.rendering.source_dom import resolve_element_path
from trans_novel.assemble.epub.rendering.source_markup import render_source_resource
from trans_novel.assemble.epub.rendering.theme import ThemeBundle
from trans_novel.assemble.epub.rendering.theme.projection import build_projection
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.epub.layout import LayoutAssignment, LayoutProfile, source_node_digest
from trans_novel.ingest.epub.reader import read_epub

_CONTAINER = b"""<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="O/content.opf"/></rootfiles></container>"""
_OPF = b"""<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title><dc:language>ja</dc:language></metadata><manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/><item id="blob" href="blob.bin" media-type="application/octet-stream"/></manifest><spine><itemref idref="c"/></spine></package>"""
_XHTML = (
    """<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" lang="ja">"""
    """<head><style id="publisher">p { color: red }</style></head>"""
    """<!--coordinate guard--><body>"""
    """<p id="plain">Plain <em>source</em><a id="ref" href="#note" """
    """role="doc-noteref"><sup>†</sup></a>.</p>"""
    """<ul><li id="container">Container <strong>source</strong>.</li></ul>"""
    """<p id="direct">One<br/>Two</p>"""
    """<p id="ruby"><ruby><rb>東</rb><rb>京</rb><rt>とう</rt><rt>きょう</rt>"""
    """</ruby><br/>After</p><p id="preserved">Preserved range.</p>"""
    """<aside id="note" role="doc-footnote"><p>Note """
    """<a href="#ref" role="doc-backlink">*</a></p></aside>"""
    """</body></html>"""
).encode()
_BLOB = b"\x00unrelated publisher bytes\xff"


class _Store:
    def __init__(self, document) -> None:
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


def _theme() -> ThemeService:
    root = etree.fromstring(_XHTML)
    projection = build_projection(root)
    assignments = tuple(
        LayoutAssignment(
            node_id=f"node-{index}",
            resource_href="O/c.xhtml",
            path=path,
            source_sha256=source_node_digest(
                node.tag.rsplit("}", 1)[-1].lower(),
                dict(node.attrib),
                "".join(node.itertext()),
            ),
            role="body",
        )
        for index, (node, path) in enumerate(zip(projection.nodes, projection.paths, strict=True))
        if projection.snapshot["nodes"][index]["isTextBlock"]
    )
    return ThemeService(
        ThemeBundle(
            general_css=b'[data-tn-role="body"] { font-family: serif; }',
            bilingual_css=b'[data-tn-content="source"] { font-size: .9em; }',
            digest="test",
            policy_version="test",
            provenance=(),
            note_markers=True,
        ),
        layout=LayoutProfile(
            source_sha256="a" * 64,
            inventory_digest="b" * 64,
            policy_version="1",
            assignments=assignments,
            provenance={},
        ),
    )


class TestSourceThemeRenderer(unittest.TestCase):
    def _book(self, path: Path) -> zipfile.ZipInfo:
        blob = zipfile.ZipInfo("O/blob.bin", date_time=(2001, 2, 3, 4, 5, 6))
        blob.compress_type = zipfile.ZIP_DEFLATED
        blob.external_attr = 0o640 << 16
        blob.extra = b"\xfe\xca\x02\x00ok"
        blob.comment = b"publisher member"
        with zipfile.ZipFile(path, "w") as archive:
            archive.comment = b"publisher archive"
            archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", _CONTAINER)
            archive.writestr("O/content.opf", _OPF)
            archive.writestr("O/c.xhtml", _XHTML)
            archive.writestr(blob, _BLOB)
        return blob

    def _store(self, source: Path):
        document = read_epub(str(source), "ja", "zh")
        translated: list[str] = []
        for segment in document.chapters[0].segments:
            if "Preserved range" in segment.source:
                segment.preserve_source = True
                continue
            values = []
            for index, slot in enumerate(segment.epub_state.slots):
                value = f"译{segment.index}-{index}" if slot.source_value.strip() else ""
                values.append({"id": slot.id, "value": value})
                if value:
                    translated.append(value)
            segment.assign_translation(values)
        return _Store(document), translated

    def _assert_zip_metadata(
        self,
        archive: zipfile.ZipFile,
        expected_info: zipfile.ZipInfo,
    ) -> bytes:
        rendered = archive.read("O/c.xhtml")
        blob_info = archive.getinfo("O/blob.bin")
        self.assertEqual(archive.comment, b"publisher archive")
        self.assertEqual(archive.read(blob_info), _BLOB)
        self.assertEqual(blob_info.date_time, expected_info.date_time)
        self.assertEqual(blob_info.compress_type, expected_info.compress_type)
        self.assertEqual(blob_info.external_attr, expected_info.external_attr)
        self.assertEqual(blob_info.extra, expected_info.extra)
        self.assertEqual(blob_info.comment, expected_info.comment)
        return rendered

    def _assert_layout_bindings(self, root: etree._Element, resource) -> None:
        self.assertTrue(resource.scope.layout_bindings)
        self.assertEqual(
            len(resource.scope.layout_bindings),
            len({binding.source_path for binding in resource.scope.layout_bindings}),
        )
        self.assertTrue(
            all(
                len(binding.source_sha256) == 64
                and binding.target_paths
                and all(
                    resolve_element_path(root, path) is not None for path in binding.target_paths
                )
                for binding in resource.scope.layout_bindings
            )
        )
        plain_binding = next(
            binding for binding in resource.scope.layout_bindings if binding.source_path == (1, 0)
        )
        self.assertEqual(
            plain_binding.source_sha256,
            source_node_digest("p", {"id": "plain"}, "Plain source†."),
        )

    def _assert_text_and_notes(self, root, resource, translated) -> None:
        preserved = resolve_element_path(root, resource.scope.excluded_paths[0])
        self.assertEqual(preserved.get("id"), "preserved")
        self.assertEqual("".join(preserved.itertext()), "Preserved range.")
        reference = root.xpath("//*[@id='ref']")[0]
        backlink = root.xpath("//*[@data-tn-note-kind='backlink']")[0]
        self.assertEqual("".join(reference.itertext()), "注")
        self.assertEqual("".join(backlink.itertext()), "注")
        self.assertTrue(
            any(
                "†" in "".join(source_node.itertext())
                for source_node in root.xpath(
                    "//*[contains(concat(' ', @class, ' '), ' tn-source ')]"
                )
            )
        )
        rendered_text = "".join(root.itertext())
        for value in translated:
            self.assertIn(value, rendered_text)
        for value in ("Plain source", "Container source.", "One", "Two", "東京とうきょう", "After"):
            self.assertIn(value, rendered_text)

    def _assert_source_pairs(self, root, resource, order) -> None:
        pairs = resource.scope.source_pairs
        self.assertEqual(
            {resolve_element_path(root, pair.source_path).get("data-tn-content") for pair in pairs},
            {"source"},
        )
        plain = next(
            pair
            for pair in pairs
            if resolve_element_path(root, pair.source_path).tag.rsplit("}", 1)[-1] == "p"
            and resolve_element_path(root, pair.source_path).xpath(".//*[local-name()='em']")
        )
        container = next(
            pair
            for pair in pairs
            if resolve_element_path(root, pair.source_path).tag.rsplit("}", 1)[-1] == "div"
        )
        ruby = next(
            pair
            for pair in pairs
            if resolve_element_path(root, pair.source_path).xpath(".//*[local-name()='ruby']")
        )
        self.assertTrue(plain.map_descendants)
        self.assertTrue(container.map_descendants)
        self.assertFalse(ruby.map_descendants)
        self.assertGreaterEqual(len(ruby.target_paths), 2)
        order_index = {
            node: index
            for index, node in enumerate(node for node in root.iter() if isinstance(node.tag, str))
        }
        for pair in pairs:
            source_node = resolve_element_path(root, pair.source_path)
            targets = [resolve_element_path(root, path) for path in pair.target_paths]
            self.assertTrue(targets)
            if all(target.get("class") == "tn-bilingual-target" for target in targets):
                self.assertTrue(
                    all(target.get("data-tn-content") == "target" for target in targets)
                )
            if any(source_node in target.iterdescendants() for target in targets):
                continue
            if order == "source_first":
                self.assertLess(
                    order_index[source_node],
                    min(order_index[target] for target in targets),
                )
            else:
                self.assertGreater(
                    order_index[source_node],
                    max(order_index[target] for target in targets),
                )

    def test_source_pairs_preserved_ranges_and_zip_metadata_survive_both_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            expected_info = self._book(source)
            source_bytes = source.read_bytes()
            store, translated = self._store(source)

            for order in ("target_first", "source_first"):
                with self.subTest(order=order):
                    output = Path(directory) / f"{order}.epub"
                    plan = assemble_source_epub(
                        store,
                        str(source),
                        str(output),
                        target_lang="zh",
                        bilingual=True,
                        order=order,
                        theme=_theme(),
                    )
                    self.assertIsNotNone(plan)
                    assert plan is not None
                    resource = next(
                        item for item in plan.resources if item.resource_href == "O/c.xhtml"
                    )
                    self.assertFalse(resource.scope.preserve_resource)
                    self.assertEqual(len(resource.scope.excluded_paths), 1)

                    with zipfile.ZipFile(output) as archive:
                        rendered = self._assert_zip_metadata(archive, expected_info)
                    root = etree.fromstring(rendered)
                    self._assert_layout_bindings(root, resource)

                    self.assertEqual(source.read_bytes(), source_bytes)
                    self.assertTrue(root.xpath("//*[local-name()='style' and @id='publisher']"))
                    self._assert_text_and_notes(root, resource, translated)

                    self._assert_source_pairs(root, resource, order)

    def test_non_chinese_target_preserves_note_labels_and_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            self._book(source)
            store, _translated = self._store(source)
            output = Path(directory) / "english.epub"

            plan = assemble_source_epub(
                store,
                str(source),
                str(output),
                target_lang="en",
                bilingual=False,
                theme=_theme(),
            )

            assert plan is not None
            self.assertIsNone(plan.source_sha256)
            with zipfile.ZipFile(output) as archive:
                root = etree.fromstring(archive.read("O/c.xhtml"))
            reference = root.xpath("//*[@id='ref']")[0]
            backlink = root.xpath("//*[@role='doc-backlink']")[0]
            self.assertEqual("".join(reference.itertext()), "†")
            self.assertEqual("".join(backlink.itertext()), "*")
            self.assertIsNone(reference.get("data-tn-note-kind"))
            self.assertEqual(reference.get("role"), "doc-noteref")

    def test_no_theme_render_skips_projection_limits(self) -> None:
        data = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            + "<span/>" * 50_001
            + "</body></html>"
        ).encode()

        rendered = render_source_resource(
            data,
            "O/large.xhtml",
            [],
            expected_digest=hashlib.sha256(data).hexdigest(),
            expected_mode="xml",
            target_lang="zh",
        )

        root = etree.fromstring(rendered)
        self.assertEqual(len(root.xpath("//*[local-name()='span']")), 50_001)

    def test_fully_preserved_resource_is_not_themed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            self._book(source)
            store, _translated = self._store(source)
            for segment in store.document.chapters[0].segments:
                segment.preserve_source = True
            output = Path(directory) / "preserved.epub"

            plan = assemble_source_epub(
                store,
                str(source),
                str(output),
                target_lang="zh",
                bilingual=True,
                theme=_theme(),
            )

            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(plan.resources, ())
            with zipfile.ZipFile(output) as archive:
                rendered = archive.read("O/c.xhtml")
                self.assertFalse(any(name.startswith("O/tn-theme/") for name in archive.namelist()))
            self.assertNotIn(b"data-tn-", rendered)
            self.assertIn(b"Preserved range.", rendered)


if __name__ == "__main__":
    unittest.main()
