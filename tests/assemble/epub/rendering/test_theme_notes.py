from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from lxml import etree

from tests.assemble.epub.rendering.test_theme_service import _chapter, _profile, _write_epub
from trans_novel.assemble.epub.rendering.theme import (
    LayoutBinding,
    NotePathMapping,
    ResourceThemeScope,
    ThemeError,
    resolve_theme,
)
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.assemble.epub.verification.theme import theme_projection


class TestThemeNotes(unittest.TestCase):
    def _assert_forged_note_plans_rejected(self, path, plan, source) -> None:
        removed_resource = replace(
            plan.resources[0],
            note_changes=plan.resources[0].note_changes[1:],
        )
        with (
            self.assertRaisesRegex(ThemeError, "^invalid_plan$"),
            theme_projection(
                path,
                replace(plan, resources=(removed_resource,)),
                source_path=source,
            ),
        ):
            pass

        namespace = plan.resources[0].note_changes[0].namespace_changes[0]
        forged_namespace = replace(
            namespace,
            after=tuple(
                (prefix, "urn:forged" if uri == "http://www.idpf.org/2007/ops" else uri)
                for prefix, uri in namespace.after
            ),
        )
        forged_change = replace(
            plan.resources[0].note_changes[0],
            namespace_changes=(forged_namespace,),
        )
        namespace_resource = replace(
            plan.resources[0],
            note_changes=(forged_change, *plan.resources[0].note_changes[1:]),
        )
        with (
            self.assertRaisesRegex(ThemeError, "^invalid_plan$"),
            theme_projection(
                path,
                replace(plan, resources=(namespace_resource,)),
                source_path=source,
            ),
        ):
            pass

        swapped_scope = replace(
            plan.resources[0].scope,
            note_paths=(
                NotePathMapping((1, 0, 1), (1, 0, 0)),
                NotePathMapping((1, 1, 0, 0), (1, 0, 1)),
                NotePathMapping((1, 1), (1, 1)),
            ),
        )
        mapping_resource = replace(plan.resources[0], scope=swapped_scope)
        with (
            self.assertRaisesRegex(ThemeError, "^invalid_plan$"),
            theme_projection(
                path,
                replace(plan, resources=(mapping_resource,)),
                source_path=source,
            ),
        ):
            pass

    def test_builtin_note_markers_use_real_text_and_preserve_link_structure(self) -> None:
        chapter = _chapter(
            '<p id="body">Body<a href="#note"><sup>†</sup></a>'
            '<a href="#note" role="doc-noteref"><sup xmlns:epub="urn:publisher">†</sup></a>.</p>'
            '<aside id="note" role="doc-footnote"><p>Footnote'
            '<a href="#body" role="doc-backlink">*</a></p></aside>'
        )
        profile = _profile(chapter)
        scope = ResourceThemeScope(
            layout_bindings=tuple(
                LayoutBinding(assignment.path, (assignment.path,), assignment.source_sha256)
                for assignment in profile.assignments
            ),
            note_paths=(
                NotePathMapping((1, 0, 1), (1, 0, 1)),
                NotePathMapping((1, 1, 0, 0), (1, 1, 0, 0)),
                NotePathMapping((1, 1), (1, 1)),
            ),
        )
        relations = {
            "version": 1,
            "markers": [
                {
                    "resource_href": "OEBPS/text/ch.xhtml",
                    "path": [1, 0, 1],
                    "kind": "noteref",
                    "label": "†",
                    "target_resource": "OEBPS/text/ch.xhtml",
                    "target_path": [1, 1],
                },
                {
                    "resource_href": "OEBPS/text/ch.xhtml",
                    "path": [1, 1, 0, 0],
                    "kind": "backlink",
                    "label": "*",
                    "target_resource": "OEBPS/text/ch.xhtml",
                    "target_path": [1, 0],
                },
            ],
            "targets": [
                {
                    "resource_href": "OEBPS/text/ch.xhtml",
                    "path": [1, 1],
                    "kind": "footnote",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.epub"
            path = Path(directory) / "output.epub"
            _write_epub(source, chapter)
            shutil.copyfile(source, path)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            service = ThemeService(resolve_theme("builtin:chinese-reading", None), layout=profile)

            plan = service.render(
                str(path),
                {"OEBPS/text/ch.xhtml": scope},
                bilingual=False,
                note_relations=relations,
                source_sha256=source_sha256,
                target_lang="zh-Hans",
            )
            assert plan is not None
            self.assertEqual(plan.source_sha256, source_sha256)
            self.assertEqual(len(plan.resources[0].note_changes), 3)
            with zipfile.ZipFile(path) as archive:
                tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
                css = archive.read(plan.resources[0].css_path).decode()
            reference = tree.xpath("(//*[local-name()='p'])[1]/*[local-name()='a'][2]")[0]
            backlink = tree.xpath("//*[@id='note']//*[local-name()='a']")[0]
            note = tree.xpath("//*[@id='note']")[0]
            self.assertTrue(plan.resources[0].note_changes[0].namespace_changes)
            self.assertEqual(("".join(reference.itertext()), reference.tail), ("注", "."))
            ordinary = tree.xpath("(//*[local-name()='p'])[1]/*[local-name()='a'][1]")[0]
            self.assertEqual("".join(ordinary.itertext()), "†")
            self.assertEqual("".join(backlink.itertext()), "注")
            self.assertEqual((reference.get("href"), backlink.get("href")), ("#note", "#body"))
            self.assertEqual(reference.get("role"), "doc-noteref")
            self.assertEqual(backlink.get("role"), "doc-backlink")
            self.assertEqual(note.get("role"), "doc-footnote")
            self.assertIn("background-color:#946126", css)
            self.assertIn("font-size:1em", css)
            self.assertIn("text-align:center", css)
            self.assertIn("margin-inline:0.15em", css)
            with theme_projection(path, plan, source_path=source) as projected:
                with zipfile.ZipFile(projected) as archive:
                    projected_tree = etree.fromstring(archive.read("OEBPS/text/ch.xhtml"))
                self.assertEqual(
                    "".join(
                        projected_tree.xpath("(//*[local-name()='p'])[1]/*[local-name()='a'][2]")[
                            0
                        ].itertext()
                    ),
                    "†",
                )
            self._assert_forged_note_plans_rejected(path, plan, source)


if __name__ == "__main__":
    unittest.main()
