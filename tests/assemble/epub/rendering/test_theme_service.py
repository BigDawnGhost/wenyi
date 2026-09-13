from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from lxml import etree

from trans_novel.assemble.epub.rendering.source_dom import parse_source_markup
from trans_novel.assemble.epub.rendering.theme import (
    InlineChange,
    LayoutBinding,
    MarkerChange,
    ResourceThemeScope,
    SourcePair,
    ThemeBundle,
    ThemeError,
)
from trans_novel.assemble.epub.rendering.theme.projection import build_projection
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.epub.layout import LayoutAssignment, LayoutProfile, source_node_digest

_CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _bundle(css: bytes, *, bilingual_css: bytes | None = None) -> ThemeBundle:
    return ThemeBundle(css, bilingual_css, "test", "test", ())


def _write_epub(
    path: Path,
    chapter: bytes,
    *,
    package_metadata: str = "",
    item_properties: str = "",
    itemref_properties: str = "",
) -> None:
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata>{package_metadata}</metadata>
  <manifest>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="chapter" href="text/ch.xhtml" media-type="application/xhtml+xml" properties="{item_properties}"/>
  </manifest>
  <spine><itemref idref="chapter" properties="{itemref_properties}"/></spine>
</package>""".encode()
    with zipfile.ZipFile(path, "w") as archive:
        mimetype = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        mimetype.compress_type = zipfile.ZIP_STORED
        archive.writestr(mimetype, b"application/epub+zip")
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/style.css", b"#publisher p { font-family: serif; }")
        archive.writestr("OEBPS/text/ch.xhtml", chapter)


def _chapter(body: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><link rel="stylesheet" type="text/css" href="../style.css"/></head>
  <body id="publisher">{body}</body>
</html>""".encode()


def _profile(
    chapter: bytes, roles: dict[tuple[int, ...], str | None] | None = None
) -> LayoutProfile:
    tree, _ = parse_source_markup(chapter)
    projection = build_projection(tree.getroot())
    assignments = []
    roles = roles or {}
    for index, (node, path) in enumerate(zip(projection.nodes, projection.paths, strict=True)):
        if not projection.snapshot["nodes"][index]["isTextBlock"]:
            continue
        role = roles.get(path, "body" if node.tag.rsplit("}", 1)[-1] == "p" else None)
        assignments.append(
            LayoutAssignment(
                node_id=f"node-{index}",
                resource_href="OEBPS/text/ch.xhtml",
                path=path,
                source_sha256=source_node_digest(
                    node.tag.rsplit("}", 1)[-1].lower(),
                    dict(node.attrib),
                    "".join(node.itertext()),
                ),
                role=role,
            )
        )
    return LayoutProfile(
        source_sha256="a" * 64,
        inventory_digest="b" * 64,
        policy_version="1",
        assignments=tuple(assignments),
        provenance={},
    )


def _service(chapter: bytes, css: bytes, *, bilingual_css: bytes | None = None) -> ThemeService:
    return ThemeService(_bundle(css, bilingual_css=bilingual_css), layout=_profile(chapter))


class TestThemeService(unittest.TestCase):
    def test_render_compiles_exact_addresses_and_applies_immutable_ledgers(self) -> None:
        chapter = _chapter(
            '<p id="target" style="font: serif !important; color: red !important">Text</p>'
            '<pre style="font-family: mono !important">Code</pre>'
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            first = Path(directory) / "first.epub"
            second = Path(directory) / "second.epub"
            _write_epub(source, chapter)
            shutil.copyfile(source, first)
            shutil.copyfile(source, second)
            service = _service(
                chapter,
                b'[data-tn-role="body"] { font-family: sans-serif; color: black; }',
            )

            first_plan = service.render(str(first), {}, bilingual=False)
            second_plan = service.render(str(second), {}, bilingual=False)

            self.assertEqual(first_plan, second_plan)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            assert first_plan is not None
            self.assertEqual(first_plan.role_counts, (("body", 1),))
            resource = first_plan.resources[0]
            self.assertEqual(resource.css_path, "OEBPS/tn-theme/style-0.css")
            with zipfile.ZipFile(first) as archive:
                css = archive.read(resource.css_path).decode()
                self.assertIn('data-tn-theme-node="n4"', css)
                tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
                target = tree.xpath("//*[local-name()='p']")[0]
                protected = tree.xpath("//*[local-name()='pre']")[0]
                self.assertEqual(target.get("data-tn-role"), "body")
                self.assertNotIn("!important", target.get("style", ""))
                self.assertIn("!important", protected.get("style", ""))
            with self.assertRaisesRegex(ThemeError, "^invalid_plan$"):
                service.apply_archive(str(first), first_plan)

    def test_profile_tampering_and_missing_layout_fail_before_writing(self) -> None:
        chapter = _chapter("<p>Text</p>")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            before = path.read_bytes()
            no_layout = ThemeService(_bundle(b'[data-tn-role="body"] { color: black; }'))
            with self.assertRaisesRegex(ThemeError, "^layout_required$"):
                no_layout.render(str(path), {}, bilingual=False)

            profile = _profile(chapter)
            no_layout.preflight_source(str(path))
            changed = replace(
                profile,
                assignments=(replace(profile.assignments[0], source_sha256="c" * 64),),
            )
            with self.assertRaisesRegex(ThemeError, "^invalid_binding$"):
                ThemeService(
                    _bundle(b'[data-tn-role="body"] { color: black; }'),
                    layout=changed,
                ).render(str(path), {}, bilingual=False)
            self.assertEqual(path.read_bytes(), before)

    def test_source_pairs_mirror_roles_without_classifying_source_copy(self) -> None:
        chapter = _chapter(
            '<p><span class="tn-bilingual-target"><em>译文</em></span></p>'
            '<span class="tn-source"><em>Source</em></span>'
        )
        profile = _profile(chapter)
        assignment = profile.assignments[0]
        scope = ResourceThemeScope(
            source_pairs=(SourcePair((1, 1), ((1, 0, 0),), map_descendants=True),),
            layout_bindings=(LayoutBinding(assignment.path, ((1, 0),), assignment.source_sha256),),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            service = ThemeService(
                _bundle(
                    b'[data-tn-role="body"] { font-family: serif; }',
                    bilingual_css=b'[data-tn-content="source"] { font-size: .9em; }',
                ),
                layout=profile,
            )
            service.render(str(path), {"OEBPS/text/ch.xhtml": scope}, bilingual=True)

            with zipfile.ZipFile(path) as archive:
                tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
            source = tree.xpath("//*[contains(concat(' ', @class, ' '), ' tn-source ')]")[0]
            direct = tree.xpath("//*[contains(concat(' ', @class, ' '), ' tn-bilingual-target ')]")[
                0
            ]
            self.assertEqual(source.get("data-tn-role"), "body")
            self.assertEqual(source.get("data-tn-content"), "source")
            self.assertEqual(direct.get("data-tn-content"), "target")
            self.assertEqual(direct.get("data-tn-role"), "body")

    def test_direct_target_does_not_cross_null_layout_owner(self) -> None:
        chapter = _chapter(
            '<blockquote id="quote"><p id="unknown">'
            '<span class="tn-bilingual-target">First<br/>Second</span>'
            "</p></blockquote>"
        )
        profile = _profile(chapter, {(1, 0): "quote", (1, 0, 0): None})
        scope = ResourceThemeScope(
            layout_bindings=tuple(
                LayoutBinding(assignment.path, (assignment.path,), assignment.source_sha256)
                for assignment in profile.assignments
            )
        )
        service = ThemeService(
            _bundle(
                b"span { color: black; }",
                bilingual_css=b'[data-tn-content="source"] { font-size: .9em; }',
            ),
            layout=profile,
        )
        with tempfile.TemporaryDirectory() as directory:
            for bilingual in (False, True):
                with self.subTest(bilingual=bilingual):
                    path = Path(directory) / f"{bilingual}.epub"
                    _write_epub(path, chapter)
                    service.render(
                        str(path),
                        {"OEBPS/text/ch.xhtml": scope},
                        bilingual=bilingual,
                    )
                    with zipfile.ZipFile(path) as archive:
                        tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
                    quote = tree.xpath("//*[@id='quote']")[0]
                    unknown = tree.xpath("//*[@id='unknown']")[0]
                    direct = tree.xpath(
                        "//*[contains(concat(' ', @class, ' '), ' tn-bilingual-target ')]"
                    )[0]
                    self.assertEqual(quote.get("data-tn-role"), "quote")
                    self.assertIsNone(unknown.get("data-tn-role"))
                    self.assertIsNone(direct.get("data-tn-role"))
                    self.assertIsNone(direct.get("data-tn-theme-node"))

    def test_broad_general_selector_applies_only_to_nonnull_roles(self) -> None:
        chapter = _chapter('<p id="known">Known</p><p id="unknown">Unknown</p>')
        profile = _profile(chapter, {(1, 0): "body", (1, 1): None})
        service = ThemeService(
            _bundle(b"p { color: black; }"),
            layout=profile,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            plan = service.render(str(path), {}, bilingual=False)

            assert plan is not None
            with zipfile.ZipFile(path) as archive:
                tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
            known = tree.xpath("//*[@id='known']")[0]
            unknown = tree.xpath("//*[@id='unknown']")[0]
            self.assertEqual(known.get("data-tn-role"), "body")
            self.assertIsNotNone(known.get("data-tn-theme-node"))
            self.assertIsNone(unknown.get("data-tn-role"))
            self.assertIsNone(unknown.get("data-tn-theme-node"))

    def test_unknown_and_note_nodes_preserve_content_and_links(self) -> None:
        chapter = _chapter(
            '<p id="unknown">Unknown</p>'
            '<a id="ref" href="#note" role="doc-noteref">1</a>'
            '<aside id="note" role="doc-footnote">Note <a href="#ref">back</a></aside>'
        )
        profile = _profile(chapter, {(1, 0): None, (1, 1): "noteref", (1, 2): "footnote"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            service = ThemeService(
                _bundle(
                    b'[data-tn-role="noteref"] { vertical-align: super; }'
                    b'[data-tn-role="footnote"] { font-size: .9em; }'
                ),
                layout=profile,
            )
            service.render(str(path), {}, bilingual=False)

            with zipfile.ZipFile(path) as archive:
                tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
            unknown = tree.xpath("//*[@id='unknown']")[0]
            reference = tree.xpath("//*[@id='ref']")[0]
            note = tree.xpath("//*[@id='note']")[0]
            self.assertIsNone(unknown.get("data-tn-role"))
            self.assertEqual((reference.text, reference.get("href")), ("1", "#note"))
            self.assertEqual(
                ("".join(note.itertext()), note[-1].get("href")), ("Note back", "#ref")
            )

    def test_fixed_layout_and_model_free_preflight_keep_archive_unchanged(self) -> None:
        chapter = _chapter("<p>Text</p>")
        package_layout = '<meta property="rendition:layout">pre-paginated</meta>'
        with tempfile.TemporaryDirectory() as directory:
            fixed = Path(directory) / "fixed.epub"
            _write_epub(fixed, chapter, package_metadata=package_layout)
            service = _service(chapter, b"p { color: black; }")
            before = fixed.read_bytes()

            service.preflight_source(str(fixed))
            plan = service.render(str(fixed), {}, bilingual=False)

            assert plan is not None
            self.assertEqual(plan.resources, ())
            self.assertIn(("fixed_layout", "OEBPS/text/ch.xhtml"), plan.warnings)
            self.assertEqual(fixed.read_bytes(), before)

    def test_apply_rebuilds_profile_and_scope_instead_of_trusting_plan(self) -> None:
        chapter = _chapter('<p style="display:none!important">Hidden</p>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            service = _service(chapter, b'[data-tn-role="body"] { color:black; }')
            plan = service.plan_archive(str(path), {}, bilingual=False)
            assert plan is not None
            before = path.read_bytes()
            resource = plan.resources[0]
            forged = replace(
                resource,
                css=resource.css.replace(b'"n4"', b'"n5"'),
                markers=(
                    *resource.markers,
                    MarkerChange((1, 1), (("data-tn-theme-node", "n5"),)),
                ),
                inline_changes=(
                    InlineChange(
                        (1, 0),
                        "display:none!important",
                        "display:none",
                        ("display",),
                    ),
                ),
            )

            with self.assertRaisesRegex(ThemeError, "^invalid_plan$"):
                service.apply_archive(str(path), replace(plan, resources=(forged,)))
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
