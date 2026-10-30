"""Whole-book evidence indexing, selection and identity tests."""

from __future__ import annotations

import json
import unittest

from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.review.conflicts import build_conflict_groups, normalize_review_issues
from wenyi_core.review.evidence import BookEvidenceIndex

from .review_fixtures import _chapter


class TestBookEvidenceIndex(unittest.TestCase):
    def setUp(self):
        self.chapters = [
            _chapter(
                0,
                [
                    ("Ann arrived.", "安到了。"),
                    ("Anna left.", "安娜走了。"),
                    ("Ann spoke.", "安开口了。"),
                ],
            ),
            _chapter(1, [("ANN returned.", "安回来了。"), ("End.", "结束。")]),
        ]
        self.term = GlossaryTerm(source="Ann", target="安", aliases=["Annie"], type="person")
        self.index = BookEvidenceIndex(
            self.chapters,
            [self.term],
            {"style_guide": "克制", "book_synopsis": "安离开后归来。"},
        )

    def test_selected_occurrences_use_book_order_alias_and_ascii_boundaries(self):
        result = self.index.term_occurrences(
            {
                "term": "Annie",
                "selectors": [1, 2, "last"],
                "context_radius": 0,
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical_term"], "Ann")
        self.assertEqual(result["total_matches"], 3)
        self.assertEqual(
            [item["ordinal"] for item in result["selected"]],
            [1, 2, 3],
        )
        selected_sources = [item["source"] for item in result["selected"]]
        self.assertNotIn("Anna left.", selected_sources)

    def test_term_tool_does_not_return_unselected_occurrences(self):
        result = self.index.term_occurrences({"term": "Ann", "selectors": [1], "context_radius": 0})
        payload = json.dumps(result, ensure_ascii=False)

        self.assertIn("Ann arrived.", payload)
        self.assertNotIn("Ann spoke.", payload)
        self.assertNotIn("ANN returned.", payload)

    def test_glossary_tool_returns_only_requested_canonical_term(self):
        result = self.index.execute(
            {
                "request_id": "glossary-1",
                "tool": "glossary_term",
                "arguments": {"term": "Annie"},
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["request_id"], "glossary-1")
        self.assertEqual(result["term"]["source"], "Ann")
        self.assertEqual(result["term"]["target"], "安")
        self.assertEqual(result["term"]["aliases"], ["Annie"])
        self.assertEqual(
            BookEvidenceIndex.evidence_refs(result),
            {result["term"]["ref"]},
        )

    def test_exact_source_wins_over_another_terms_same_alias(self):
        other = GlossaryTerm(source="Anne", target="安妮", aliases=["Ann"], type="person")
        index = BookEvidenceIndex(self.chapters, [self.term, other], {})

        term, ambiguous = index.canonical_term("Ann")

        self.assertIs(term, self.term)
        self.assertEqual(ambiguous, [])

    def test_exact_case_sensitive_source_wins_and_normalized_collision_is_ambiguous(self):
        upper = GlossaryTerm(source="ANN", target="甲", aliases=["Alice"], type="person")
        title = GlossaryTerm(source="Ann", target="乙", aliases=["Annie"], type="person")
        index = BookEvidenceIndex(
            [_chapter(0, [("Alice arrived.", "甲到了。"), ("Annie left.", "乙走了。")])],
            [upper, title],
            {},
        )

        self.assertIs(index.canonical_term("ANN")[0], upper)
        self.assertIs(index.canonical_term("Ann")[0], title)
        term, ambiguous = index.canonical_term("ann")
        self.assertIsNone(term)
        self.assertEqual(ambiguous, ["ANN", "Ann"])
        self.assertNotEqual(
            index.glossary_term({"term": "ANN"})["term"]["ref"],
            index.glossary_term({"term": "Ann"})["term"]["ref"],
        )
        self.assertEqual(
            index.term_occurrences({"term": "ANN", "selectors": [1]})["selected"][0]["source"],
            "Alice arrived.",
        )
        self.assertEqual(
            index.term_occurrences({"term": "Ann", "selectors": [1]})["selected"][0]["source"],
            "Annie left.",
        )

    def test_occurrence_result_includes_only_the_matched_glossary_entry(self):
        result = self.index.term_occurrences({"term": "Ann", "selectors": [1], "context_radius": 0})

        self.assertEqual(result["glossary_term"]["source"], "Ann")
        self.assertEqual(result["glossary_term"]["target"], "安")
        self.assertIn(result["glossary_term"]["ref"], BookEvidenceIndex.evidence_refs(result))

    def test_distinct_exact_sources_are_not_merged_into_one_conflict_key(self):
        upper = GlossaryTerm(source="ANN", target="甲", type="person")
        title = GlossaryTerm(source="Ann", target="乙", type="person")
        evidence = BookEvidenceIndex(self.chapters, [upper, title], {})
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"chunk-{index}",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": proposed,
                    "consistency": {
                        "kind": "term",
                        "subject_source": source,
                        "proposed_value": proposed,
                    },
                }
                for index, (source, proposed) in enumerate((("ANN", "甲"), ("Ann", "乙")))
            ],
            evidence,
        )

        self.assertNotEqual(
            issues[0]["consistency"]["key"],
            issues[1]["consistency"]["key"],
        )
        self.assertEqual(build_conflict_groups(issues), [])

        ambiguous_issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"ambiguous-{index}",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": proposed,
                    "consistency": {
                        "kind": "term",
                        "subject_source": "ann",
                        "proposed_value": proposed,
                    },
                }
                for index, proposed in enumerate(("甲", "乙"))
            ],
            evidence,
        )
        self.assertTrue(
            all(issue["consistency"]["auto_arbitration"] is False for issue in ambiguous_issues)
        )
        self.assertEqual(build_conflict_groups(ambiguous_issues), [])

    def test_book_context_has_stable_refs_and_rejects_unknown_chapter(self):
        style = self.index.execute(
            {
                "request_id": "style-1",
                "tool": "book_context",
                "arguments": {"section": "style_guide"},
            }
        )
        digest = self.index.execute(
            {
                "request_id": "digest-1",
                "tool": "book_context",
                "arguments": {"section": "chapter_digest", "chapter": 1},
            }
        )
        unknown = self.index.book_context({"section": "chapter_digest", "chapter": 99})

        self.assertEqual(BookEvidenceIndex.evidence_refs(style), {"book:style_guide"})
        self.assertEqual(
            BookEvidenceIndex.evidence_refs(digest),
            {"book:chapter_digest:ch1"},
        )
        self.assertEqual(unknown, {"ok": False, "error": "chapter_not_found"})

    def test_oversized_evidence_result_is_rejected(self):
        long = "x" * 5000
        index = BookEvidenceIndex(
            [_chapter(0, [(f"Ann {i} {long}", long) for i in range(8)])],
            [self.term],
            {},
        )
        result = index.execute(
            {
                "request_id": "large-1",
                "tool": "term_occurrences",
                "arguments": {
                    "term": "Ann",
                    "selectors": list(range(1, 9)),
                    "context_radius": 2,
                },
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "evidence_result_too_large")

    def test_segment_context_crosses_chapter_boundary(self):
        result = self.index.segment_context({"chapter": 1, "index": 0, "before": 1, "after": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(
            [segment["source"] for segment in result["segments"]],
            ["Ann spoke.", "ANN returned.", "End."],
        )

    def test_target_overrides_are_visible_without_mutating_chapters(self):
        original = self.chapters[0].text_segments[0].target
        index = BookEvidenceIndex(
            self.chapters,
            [self.term],
            {},
            target_overrides={(0, 0): "影子修订。"},
        )

        context = index.segment_context({"chapter": 0, "index": 0, "before": 0, "after": 0})

        self.assertEqual(index.segments[0].target, "影子修订。")
        self.assertEqual(context["segments"][0]["target"], "影子修订。")
        self.assertEqual(context["segments"][0]["target_origin"], "shadow_override")
        self.assertEqual(context["segments"][0]["baseline_target"], original)
        self.assertEqual(self.chapters[0].text_segments[0].target, original)

    def test_formal_targets_are_labeled_without_duplicate_baseline_payload(self):
        context = self.index.segment_context({"chapter": 0, "index": 0, "before": 0, "after": 0})

        segment = context["segments"][0]
        self.assertEqual(segment["target_origin"], "formal")
        self.assertNotIn("baseline_target", segment)
