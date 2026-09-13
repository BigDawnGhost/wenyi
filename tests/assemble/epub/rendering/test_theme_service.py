from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from lxml import etree

from trans_novel.assemble.epub.rendering.theme import (
    InlineChange,
    MarkerChange,
    ResourceThemeScope,
    SourcePair,
    ThemeBundle,
    ThemeError,
)
from trans_novel.assemble.epub.rendering.theme.service import ThemeService

_CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _bundle(css: bytes, *, bilingual_css: bytes | None = None) -> ThemeBundle:
    return ThemeBundle(
        script=b"function classify(node) { return node.tag === 'p' ? {role: 'body'} : null; }",
        general_css=css,
        bilingual_css=bilingual_css,
        digest="test",
        engine_version="test",
        api_version=1,
        policy_version="test",
        provenance=(),
    )


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
            service = ThemeService(
                _bundle(b'[data-tn-role="body"] { font-family: sans-serif; color: black; }')
            )

            first_plan = service.render(str(first), {}, bilingual=False)
            second_plan = service.render(str(second), {}, bilingual=False)

            self.assertIsNotNone(first_plan)
            self.assertEqual(first_plan, second_plan)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            assert first_plan is not None
            self.assertEqual(first_plan.members[0][0], "mimetype")
            self.assertEqual(first_plan.role_counts, (("body", 1),))
            resource = first_plan.resources[0]
            self.assertEqual(resource.css_path, "OEBPS/tn-theme/style-0.css")
            self.assertEqual(resource.css_id, "tn-theme-style-0")
            self.assertEqual(resource.link_attributes[-1], ("href", "../tn-theme/style-0.css"))
            self.assertEqual(resource.inline_changes[0].declarations, ("font", "color"))
            with zipfile.ZipFile(first) as archive:
                css = archive.read(resource.css_path).decode()
                self.assertIn('[data-tn-theme-node="n4"]', css)
                self.assertIn(":not(#tn-theme-never)", css)
                self.assertIn("font-family:sans-serif !important", css)
                tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
                target = tree.xpath("//*[local-name()='p']")[0]
                protected = tree.xpath("//*[local-name()='pre']")[0]
                self.assertEqual(target.get("data-tn-role"), "body")
                self.assertEqual(target.get("data-tn-content"), "target")
                self.assertNotIn("!important", target.get("style", ""))
                self.assertIn("!important", protected.get("style", ""))
                opf = etree.fromstring(archive.read("OEBPS/content.opf"))
                generated = opf.xpath("//*[local-name()='item' and @id='tn-theme-style-0']")
                self.assertEqual(generated[0].get("href"), "tn-theme/style-0.css")
            with self.assertRaises(ThemeError) as caught:
                service.apply_archive(str(first), first_plan)
            self.assertEqual(
                (caught.exception.code, str(caught.exception)), ("theme_verify", "invalid_plan")
            )

    def test_source_pairs_map_roles_without_classifying_source_text(self) -> None:
        chapter = _chapter(
            '<p><span class="tn-bilingual-target"><em>译文</em></span></p>'
            '<span class="tn-source"><em>Source</em></span>'
        )
        scope = ResourceThemeScope(
            source_pairs=(SourcePair((1, 1), ((1, 0, 0),), map_descendants=True),)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            service = ThemeService(
                _bundle(
                    b'[data-tn-role="body"] { font-family: serif; }',
                    bilingual_css=b'[data-tn-content="source"] { font-size: .9em; }',
                )
            )

            plan = service.render(str(path), {"OEBPS/text/ch.xhtml": scope}, bilingual=True)

            assert plan is not None
            with zipfile.ZipFile(path) as archive:
                tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
            source = tree.xpath("//*[contains(concat(' ', @class, ' '), ' tn-source ')]")[0]
            self.assertEqual(source.get("data-tn-role"), "body")
            self.assertEqual(source.get("data-tn-content"), "source")
            direct = tree.xpath("//*[contains(concat(' ', @class, ' '), ' tn-bilingual-target ')]")[
                0
            ]
            self.assertEqual(direct.get("data-tn-content"), "target")
            self.assertIsNone(direct.get("data-tn-role"))

    def test_media_failure_and_reserved_collision_leave_archive_unchanged(self) -> None:
        chapter = _chapter('<p style="font-family: serif !important">Text</p>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            before = path.read_bytes()
            service = ThemeService(
                _bundle(
                    b"@media (prefers-color-scheme: dark) {"
                    b'[data-tn-role="body"] { font-family: sans-serif; }}'
                )
            )
            with self.assertRaises(ThemeError) as caught:
                service.render(str(path), {}, bilingual=False)
            self.assertEqual(str(caught.exception), "media_coverage_required")
            self.assertEqual(path.read_bytes(), before)

            collided = chapter.replace(b"<p style=", b'<p data-tn-role="body" style=')
            _write_epub(path, collided)
            collision_before = path.read_bytes()
            with self.assertRaises(ThemeError) as caught:
                ThemeService(_bundle(b"p { color: black; }")).render(str(path), {}, bilingual=False)
            self.assertEqual(
                (caught.exception.code, str(caught.exception)),
                ("theme_collision", "reserved_marker"),
            )
            self.assertEqual(path.read_bytes(), collision_before)

    def test_fixed_layout_is_skipped_but_reflowable_itemref_overrides_package(self) -> None:
        chapter = _chapter("<p>Text</p>")
        package_layout = '<meta property="rendition:layout">pre-paginated</meta>'
        with tempfile.TemporaryDirectory() as directory:
            fixed = Path(directory) / "fixed.epub"
            flowing = Path(directory) / "flowing.epub"
            _write_epub(fixed, chapter, package_metadata=package_layout)
            _write_epub(
                flowing,
                chapter,
                package_metadata=package_layout,
                itemref_properties="rendition:layout-reflowable",
            )
            service = ThemeService(_bundle(b"p { color: black; }"))

            fixed_before = fixed.read_bytes()
            fixed_plan = service.render(str(fixed), {}, bilingual=False)
            flowing_plan = service.render(str(flowing), {}, bilingual=False)

            assert fixed_plan is not None and flowing_plan is not None
            self.assertEqual(fixed_plan.resources, ())
            self.assertIn(("fixed_layout", "OEBPS/text/ch.xhtml"), fixed_plan.warnings)
            self.assertEqual(fixed.read_bytes(), fixed_before)
            self.assertEqual(len(flowing_plan.resources), 1)

    def test_apply_rebuilds_scope_protection_without_cached_planning_state(self) -> None:
        chapter = _chapter('<p style="color: red !important">Text</p>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            service = ThemeService(_bundle(b"p { color: black; }"))
            plan = service.plan_archive(str(path), {}, bilingual=False)
            assert plan is not None
            before = path.read_bytes()
            resource = plan.resources[0]
            protected_resource = replace(
                resource,
                scope=ResourceThemeScope(excluded_paths=((1, 0),)),
            )
            protected_plan = replace(plan, resources=(protected_resource,))

            with self.assertRaises(ThemeError) as caught:
                service.apply_archive(str(path), protected_plan)

            self.assertEqual(
                (caught.exception.code, str(caught.exception)),
                ("theme_verify", "invalid_plan"),
            )
            self.assertEqual(path.read_bytes(), before)

    def test_rejects_redirected_css_and_unrelated_inline_demotions(self) -> None:
        chapter = _chapter('<p style="display:none!important">Hidden</p><div>Other</div>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, chapter)
            service = ThemeService(_bundle(b"p { color:black; }"))
            plan = service.plan_archive(str(path), {}, bilingual=False)
            assert plan is not None
            before = path.read_bytes()
            resource = plan.resources[0]
            redirected = replace(
                resource,
                css=resource.css.replace(b'"n4"', b'"n5"'),
                markers=(*resource.markers, MarkerChange((1, 1), (("data-tn-theme-node", "n5"),))),
            )
            demoted = replace(
                resource,
                inline_changes=(
                    InlineChange((1, 0), "display:none!important", "display:none", ("display",)),
                ),
            )
            for forged in (redirected, demoted):
                path.write_bytes(before)
                with self.subTest(css=forged is redirected):
                    with self.assertRaisesRegex(ThemeError, "^invalid_plan$"):
                        service.apply_archive(str(path), replace(plan, resources=(forged,)))
                    self.assertEqual(path.read_bytes(), before)

    def test_script_failure_reports_resource_without_disclosing_thrown_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.epub"
            _write_epub(path, _chapter("<p>Text</p>"))
            service = ThemeService(
                replace(
                    _bundle(b"p { color:black; }"),
                    script=b"function classify() { throw 'private'; }",
                )
            )
            before = path.read_bytes()
            with self.assertRaises(ThemeError) as raised:
                service.render(str(path), {}, bilingual=False)
            self.assertEqual(raised.exception.resource, "OEBPS/text/ch.xhtml")
            self.assertEqual(raised.exception.node_id, 0)
            self.assertEqual(str(raised.exception), "classification_failed")
            self.assertEqual(path.read_bytes(), before)
