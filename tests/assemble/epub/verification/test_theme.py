from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from bs4 import BeautifulSoup
from lxml import etree

from tests.fixtures.books import write_phase9_epub
from trans_novel.assemble.epub.rendering.source_dom import resolve_element_path
from trans_novel.assemble.epub.rendering.theme import (
    LayoutBinding,
    ResourceThemeScope,
    SourcePair,
    ThemeBundle,
)
from trans_novel.assemble.epub.rendering.theme.projection import build_projection
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.assemble.epub.verification import validate_epub_triplet, verify_epub
from trans_novel.epub.layout import LayoutAssignment, LayoutProfile, source_node_digest


def _service(
    path: Path, scopes: dict[str, ResourceThemeScope]
) -> tuple[ThemeService, dict[str, ResourceThemeScope]]:
    assignments: list[LayoutAssignment] = []
    bindings: dict[str, list[LayoutBinding]] = {}
    with zipfile.ZipFile(path) as archive:
        for resource in archive.namelist():
            if not resource.endswith((".xhtml", ".html", ".htm")):
                continue
            root = etree.fromstring(archive.read(resource))
            projection = build_projection(root)
            for index, (node, node_path) in enumerate(
                zip(projection.nodes, projection.paths, strict=True)
            ):
                if not projection.snapshot["nodes"][index]["isTextBlock"]:
                    continue
                digest = source_node_digest(
                    node.tag.rsplit("}", 1)[-1].lower(),
                    dict(node.attrib),
                    "".join(node.itertext()),
                )
                assignments.append(
                    LayoutAssignment(
                        node_id=f"{resource}-{index}",
                        resource_href=resource,
                        path=node_path,
                        source_sha256=digest,
                        role="body",
                    )
                )
                bindings.setdefault(resource, []).append(
                    LayoutBinding(node_path, (node_path,), digest)
                )
    bound_scopes = {
        resource: replace(scope, layout_bindings=tuple(bindings.get(resource, ())))
        for resource, scope in scopes.items()
    }
    profile = LayoutProfile(
        source_sha256="a" * 64,
        inventory_digest="b" * 64,
        policy_version="1",
        assignments=tuple(assignments),
        provenance={},
    )
    service = ThemeService(
        ThemeBundle(
            general_css=b'[data-tn-role="body"] { color: black; font-family: serif; }',
            bilingual_css=b'[data-tn-content="source"] { font-size: .9em; }',
            digest="test",
            policy_version="test",
            provenance=(),
        ),
        layout=profile,
    )
    return service, bound_scopes


def _rewrite(
    path: Path,
    transform,
    *,
    omit: str | None = None,
    duplicate: str | None = None,
) -> None:
    temporary = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as output:
        output.comment = source.comment
        for info in source.infolist():
            if info.filename == omit:
                continue
            data = transform(info.filename, source.read(info.filename))
            output.writestr(info, data)
            if info.filename == duplicate:
                output.writestr(info, data)
    os.replace(temporary, path)


def _themed(root: Path, *, bilingual: bool = False, inline: bool = False):
    path = root / "output.epub"
    write_phase9_epub(str(path), long_chapter_chars=100)
    if inline:

        def add_inline(name: str, data: bytes) -> bytes:
            if name == "OEBPS/text/chapter-1.xhtml":
                return data.replace(
                    b'<p id="intro">',
                    b'<p id="intro" style="color:red !important">',
                )
            return data

        _rewrite(path, add_inline)
    scopes = {}
    if bilingual:

        def add_bilingual_source(name: str, data: bytes) -> bytes:
            if name != "OEBPS/text/chapter-2.xhtml":
                return data
            soup = BeautifulSoup(data, "xml")
            body = soup.find(id="body-two")
            assert body is not None
            source_node = soup.new_tag("p", attrs={"class": "tn-source"})
            source_node.string = "Second chapter."
            body.insert_after(source_node)
            return str(soup).encode()

        _rewrite(path, add_bilingual_source)
        scopes = {
            "OEBPS/text/chapter-2.xhtml": ResourceThemeScope(
                source_pairs=(SourcePair((1, 2), ((1, 1),)),)
            )
        }
    service, scopes = _service(path, scopes)
    plan = service.render(str(path), scopes, bilingual=bilingual)
    assert plan is not None and plan.resources
    return path, plan


class TestThemeVerification(unittest.TestCase):
    def test_planned_mono_and_bilingual_outputs_pass_with_actual_hashes(self) -> None:
        for bilingual in (False, True):
            with self.subTest(bilingual=bilingual), tempfile.TemporaryDirectory() as directory:
                path, plan = _themed(Path(directory), bilingual=bilingual)
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()

                report = verify_epub(path, bilingual=bilingual, theme_plan=plan)

                self.assertTrue(report["passed"], report["failures"])
                self.assertEqual(report["output_sha256"], actual_hash)
                self.assertEqual(report["theme"]["resources"], len(plan.resources))

    def test_triplet_projects_both_expected_plans_and_binds_actual_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            mono = root / "mono.epub"
            bilingual = root / "bilingual.epub"
            write_phase9_epub(str(source), long_chapter_chars=100)
            shutil.copyfile(source, mono)

            def translate(name: str, data: bytes) -> bytes:
                if name != "OEBPS/text/chapter-2.xhtml":
                    return data
                soup = BeautifulSoup(data, "xml")
                body = soup.find(id="body-two")
                assert body is not None
                body.string = "Translated second chapter."
                return str(soup).encode()

            _rewrite(mono, translate)
            shutil.copyfile(mono, bilingual)

            def add_source(name: str, data: bytes) -> bytes:
                if name != "OEBPS/text/chapter-2.xhtml":
                    return data
                soup = BeautifulSoup(data, "xml")
                body = soup.find(id="body-two")
                assert body is not None
                source_node = soup.new_tag("p", attrs={"class": "tn-source"})
                source_node.string = "Second chapter."
                body.insert_after(source_node)
                return str(soup).encode()

            _rewrite(bilingual, add_source)
            mono_service, mono_scopes = _service(mono, {})
            mono_plan = mono_service.render(str(mono), mono_scopes, bilingual=False)
            bilingual_scopes = {
                "OEBPS/text/chapter-2.xhtml": ResourceThemeScope(
                    source_pairs=(SourcePair((1, 2), ((1, 1),)),)
                )
            }
            bilingual_service, bilingual_scopes = _service(bilingual, bilingual_scopes)
            bilingual_plan = bilingual_service.render(
                str(bilingual),
                bilingual_scopes,
                bilingual=True,
            )
            assert mono_plan is not None and bilingual_plan is not None

            result = validate_epub_triplet(
                source,
                mono,
                bilingual,
                mono_theme_plan=mono_plan,
                bilingual_theme_plan=bilingual_plan,
            )

            self.assertTrue(result["structural_pass"], result)
            self.assertEqual(result["mono"]["theme"]["resources"], len(mono_plan.resources))
            self.assertEqual(
                result["bilingual"]["theme"]["resources"], len(bilingual_plan.resources)
            )
            self.assertEqual(
                result["mono"]["path_sha256"], hashlib.sha256(mono.read_bytes()).hexdigest()
            )
            self.assertEqual(
                result["bilingual"]["path_sha256"],
                hashlib.sha256(bilingual.read_bytes()).hexdigest(),
            )

    def test_css_member_and_link_tampering_fail_closed(self) -> None:
        mutations = ("altered_css", "missing_css", "duplicate_css", "duplicate_link")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                path, plan = _themed(Path(directory))
                resource = plan.resources[0]

                def transform(
                    name: str, data: bytes, *, mutation=mutation, resource=resource
                ) -> bytes:
                    if mutation == "altered_css" and name == resource.css_path:
                        return data + b"x"
                    if mutation == "duplicate_link" and name == resource.resource_href:
                        tree = etree.fromstring(data)
                        head = resolve_element_path(tree, resource.head_path)
                        head.append(deepcopy(list(head)[-1]))
                        return etree.tostring(tree)
                    return data

                _rewrite(
                    path,
                    transform,
                    omit=resource.css_path if mutation == "missing_css" else None,
                    duplicate=resource.css_path if mutation == "duplicate_css" else None,
                )
                report = verify_epub(path, theme_plan=plan)
                self.assertFalse(report["passed"])
                self.assertEqual({item["code"] for item in report["failures"]}, {"theme_verify"})

    def test_marker_inline_manifest_and_original_text_tampering_fail_closed(self) -> None:
        mutations = ("unexpected_marker", "inline", "manifest", "original_text")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                path, plan = _themed(Path(directory), inline=mutation == "inline")
                resource = next(
                    item for item in plan.resources if item.inline_changes or mutation != "inline"
                )

                def transform(
                    name: str, data: bytes, *, mutation=mutation, resource=resource, plan=plan
                ) -> bytes:
                    if name == resource.resource_href and mutation != "manifest":
                        tree = etree.fromstring(data)
                        if mutation == "unexpected_marker":
                            tree.set("data-tn-role", "unexpected")
                        elif mutation == "inline":
                            change = resource.inline_changes[0]
                            resolve_element_path(tree, change.path).set("style", change.after + " ")
                        else:
                            node = next(
                                node for node in tree.iter() if node.text and node.text.strip()
                            )
                            node.text = f"{node.text}changed"
                        return etree.tostring(tree)
                    if name == plan.opf_path and mutation == "manifest":
                        tree = etree.fromstring(data)
                        item = tree.xpath(
                            "//*[local-name()='item' and @id=$identifier]",
                            identifier=resource.css_id,
                        )[0]
                        item.set("media-type", "text/plain")
                        return etree.tostring(tree)
                    return data

                _rewrite(path, transform)
                report = verify_epub(path, theme_plan=plan)
                self.assertFalse(report["passed"])
                self.assertEqual({item["code"] for item in report["failures"]}, {"theme_verify"})

    def test_bilingual_flag_mismatch_fails_without_exposing_plan_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, plan = _themed(Path(directory), bilingual=True)

            report = verify_epub(path, bilingual=False, theme_plan=plan)

            self.assertFalse(report["passed"])
            self.assertIsNone(report["theme"])
            self.assertEqual(report["failures"][0]["path"], "<archive>")
