from __future__ import annotations

import unittest

from lxml import etree

from trans_novel.epub.notes import detect_note_relations


class TestNoteRelations(unittest.TestCase):
    def _root(self, body: str) -> etree._Element:
        return etree.fromstring(
            (
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                f"{body}</body></html>"
            ).encode()
        )

    def test_symbol_pair_supports_nested_marker_and_split_empty_identifier(self) -> None:
        root = self._root(
            '<p>Body<a id="ref"/><sup><a href="#note"><span>†</span></a></sup> tail</p>'
            '<p id="note"><a href="#ref">†</a> Note body</p>'
        )
        before = etree.tostring(root)

        relations = detect_note_relations({"chapter.xhtml": root})

        self.assertEqual(
            [(marker["kind"], marker["label"]) for marker in relations["markers"]],
            [("noteref", "†"), ("backlink", "†")],
        )
        self.assertEqual(relations["targets"][0]["kind"], "footnote")
        self.assertEqual(etree.tostring(root), before)

    def test_numeric_pair_requires_note_resource_or_element_semantics(self) -> None:
        plain = self._root(
            '<p><a id="ref" href="#note">[12]</a></p><p id="note"><a href="#ref">12</a> Note</p>'
        )
        semantic = self._root(
            '<p><a id="ref" href="#note">[12]</a></p>'
            '<aside id="note" epub:type="endnote"><a href="#ref">12</a> Note</aside>'
        )

        self.assertEqual(detect_note_relations({"plain.xhtml": plain})["markers"], [])
        contextual = detect_note_relations(
            {"plain.xhtml": plain}, note_resources={"plain.xhtml": "footnote"}
        )
        self.assertEqual(len(contextual["markers"]), 2)
        self.assertEqual(contextual["targets"][0]["kind"], "footnote")
        detected = detect_note_relations({"semantic.xhtml": semantic})
        self.assertEqual(len(detected["markers"]), 2)
        self.assertEqual(detected["targets"][0]["kind"], "endnote")

    def test_multiple_symbol_backlinks_are_preserved(self) -> None:
        root = self._root(
            '<p>One<a id="r1" href="#note">*</a> Two<a id="r2" href="#note">*</a></p>'
            '<p id="note"><a href="#r1">*</a> <a href="#r2">*</a> Note</p>'
        )

        relations = detect_note_relations({"chapter.xhtml": root})

        self.assertEqual(
            [marker["kind"] for marker in relations["markers"]],
            ["noteref", "backlink", "noteref", "backlink"],
        )
        self.assertEqual(len(relations["targets"]), 1)

    def test_explicit_roles_resolve_cross_resource_and_preserve_labels(self) -> None:
        chapter = self._root(
            '<p>Body<a id="ref" role="doc-noteref" href="notes.xhtml#note">note A</a></p>'
        )
        notes = self._root(
            '<aside id="note" role="doc-endnote">'
            '<a role="doc-backlink" href="chapter.xhtml#ref">return</a> Note</aside>'
        )

        relations = detect_note_relations({"chapter.xhtml": chapter, "notes.xhtml": notes})

        self.assertEqual(
            [(marker["kind"], marker["label"]) for marker in relations["markers"]],
            [("noteref", "note A"), ("backlink", "return")],
        )
        self.assertEqual(
            relations["targets"],
            [
                {
                    "resource_href": "notes.xhtml",
                    "path": [0, 0],
                    "kind": "endnote",
                }
            ],
        )

    def test_ambiguous_broken_unsafe_and_navigation_candidates_are_omitted(self) -> None:
        source = self._root(
            '<p><a id="ref" href="#duplicate">*</a></p>'
            '<p id="duplicate"><a href="#ref">*</a> First</p>'
            '<p id="duplicate"><a href="#ref">*</a> Second</p>'
            '<p><a href="https://example.com/#note">†</a></p>'
            '<p><a href="../missing.xhtml#note">‡</a></p>'
            '<nav><p><a id="nav-ref" href="#nav-note">*</a></p>'
            '<p id="nav-note"><a href="#nav-ref">*</a> Nav</p></nav>'
        )

        relations = detect_note_relations({"chapter.xhtml": source})

        self.assertEqual(relations, {"version": 1, "markers": [], "targets": []})

    def test_words_classes_and_non_note_numeric_loops_are_not_authority(self) -> None:
        root = self._root(
            '<p><a id="word-ref" class="noteref" href="#word-note">note</a></p>'
            '<p id="word-note" class="footnote"><a href="#word-ref">note</a> Text</p>'
            '<p><a id="number-ref" href="#number-note">3</a></p>'
            '<p id="number-note"><a href="#number-ref">3</a> Cross reference</p>'
            '<p><a id="mismatch-ref" href="#mismatch-note">*</a></p>'
            '<p id="mismatch-note"><a href="#mismatch-ref">†</a> Mismatch</p>'
            '<p><a id="unicode-ref" href="#unicode-note">٣</a></p>'
            '<aside id="unicode-note" role="doc-footnote">'
            '<a href="#unicode-ref">٣</a> Non-ASCII number</aside>'
        )

        self.assertEqual(
            detect_note_relations({"chapter.xhtml": root}),
            {"version": 1, "markers": [], "targets": []},
        )

    def test_reciprocal_start_markers_with_both_directions_are_ambiguous(self) -> None:
        root = self._root(
            '<p><a id="left" href="#right">†</a> Left</p>'
            '<p><a id="right" href="#left">†</a> Right</p>'
        )

        self.assertEqual(
            detect_note_relations({"chapter.xhtml": root}),
            {"version": 1, "markers": [], "targets": []},
        )

    def test_excluded_resource_is_not_scanned(self) -> None:
        root = self._root(
            '<p><a id="ref" href="#note">*</a></p><p id="note"><a href="#ref">*</a> Note</p>'
        )

        self.assertEqual(
            detect_note_relations(
                {"navigation.xhtml": root}, excluded_resources={"navigation.xhtml"}
            ),
            {"version": 1, "markers": [], "targets": []},
        )


if __name__ == "__main__":
    unittest.main()
