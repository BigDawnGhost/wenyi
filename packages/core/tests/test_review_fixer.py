"""Provisional review patch validation and protocol tests."""

from __future__ import annotations

import json
import unittest

from wenyi_core.agents.review_fixer import (
    ProvisionalPatch,
    ReviewFixer,
    ReviewFixerProtocolError,
)
from wenyi_core.llm.providers.fake import FakeClient

from .review_fixtures import _config


class TestReviewFixer(unittest.TestCase):
    def _propose_raw(self, raw: str) -> ProvisionalPatch:
        client = FakeClient(handler=lambda messages, tier, json_mode: raw)
        return ReviewFixer(client, _config()).propose(
            1,
            "ch0:text1:seg1",
            0,
            1,
            "Original sentence.",
            "当前译文。",
            [
                {
                    "issue_id": "r1-review-00001",
                    "chapter": 0,
                    "index": 1,
                    "type": "mistranslation",
                    "detail": "原意不完整",
                    "suggestion": "补全信息",
                }
            ],
        )

    def _propose(self, payload: dict) -> ProvisionalPatch:
        return self._propose_raw(json.dumps(payload, ensure_ascii=False))

    def _valid_payload(self, replacement: str = "修订后的完整译文。") -> dict:
        return {
            "segment_ref": "ch0:text1:seg1",
            "before_hash": ReviewFixer.target_hash("当前译文。"),
            "issue_ids": ["r1-review-00001"],
            "replacement": replacement,
            "complete": True,
        }

    def test_valid_full_segment_patch_is_provisional(self):
        patch = self._propose(self._valid_payload())

        self.assertEqual(patch.before, "当前译文。")
        self.assertEqual(patch.after, "修订后的完整译文。")
        self.assertEqual(patch.issue_ids, ("r1-review-00001",))
        self.assertEqual(patch.status, "provisional")

    def test_complete_can_precede_other_fixer_fields(self):
        payload = {
            "complete": True,
            "segment_ref": "ch0:text1:seg1",
            "before_hash": ReviewFixer.target_hash("当前译文。"),
            "issue_ids": ["r1-review-00001"],
            "replacement": "修订后的完整译文。",
        }

        patch = self._propose(payload)

        self.assertEqual(patch.after, "修订后的完整译文。")

    def test_rejects_missing_or_non_true_completion_marker(self):
        missing = self._valid_payload()
        del missing["complete"]
        non_true = self._valid_payload()
        non_true["complete"] = False

        for payload, reason in (
            (missing, "unexpected_fields"),
            (non_true, "completion_marker_missing"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ReviewFixerProtocolError, reason):
                    self._propose(payload)

    def test_rejects_truncated_replacement_after_completion_marker(self):
        before_hash = ReviewFixer.target_hash("当前译文。")
        raw = (
            '{"complete":true,"segment_ref":"ch0:text1:seg1",'
            f'"before_hash":"{before_hash}",'
            '"issue_ids":["r1-review-00001"],'
            '"replacement":"修订后的完整译文'
        )

        with self.assertRaisesRegex(ReviewFixerProtocolError, "unsafe_json_repair"):
            self._propose_raw(raw)

    def test_rejects_protocol_drift_and_unchanged_replacement(self):
        wrong_segment = self._valid_payload()
        wrong_segment["segment_ref"] = "ch0:text1:seg2"
        wrong_hash = self._valid_payload()
        wrong_hash["before_hash"] = ReviewFixer.target_hash("另一版译文。")
        wrong_ids = self._valid_payload()
        wrong_ids["issue_ids"] = ["another-issue"]
        extra_field = self._valid_payload()
        extra_field["explanation"] = "不允许"

        for payload, reason in (
            (wrong_segment, "segment_ref_mismatch"),
            (wrong_hash, "before_hash_mismatch"),
            (wrong_ids, "issue_ids_mismatch"),
            (extra_field, "unexpected_fields"),
            (self._valid_payload("当前译文。"), "unchanged_replacement"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ReviewFixerProtocolError, reason):
                    self._propose(payload)

    def test_rejects_dropped_dialogue_quotes(self):
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                {
                    "segment_ref": "ch0:text1:seg1",
                    "before_hash": ReviewFixer.target_hash("“当前译文。”"),
                    "issue_ids": ["r1-review-00001"],
                    "replacement": "修订后的完整译文。",
                    "complete": True,
                },
                ensure_ascii=False,
            )
        )

        with self.assertRaisesRegex(
            ReviewFixerProtocolError,
            "dropped_dialogue_quotes",
        ):
            ReviewFixer(client, _config()).propose(
                1,
                "ch0:text1:seg1",
                0,
                1,
                '"Original sentence."',
                "“当前译文。”",
                [
                    {
                        "issue_id": "r1-review-00001",
                        "chapter": 0,
                        "index": 1,
                        "type": "mistranslation",
                        "detail": "原意不完整",
                        "suggestion": "补全信息",
                    }
                ],
            )

    def test_allows_removing_target_quotes_absent_from_source(self):
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                {
                    "segment_ref": "ch0:text1:seg1",
                    "before_hash": ReviewFixer.target_hash("“当前译文。”"),
                    "issue_ids": ["r1-review-00001"],
                    "replacement": "修订后的完整译文。",
                    "complete": True,
                },
                ensure_ascii=False,
            )
        )

        patch = ReviewFixer(client, _config()).propose(
            1,
            "ch0:text1:seg1",
            0,
            1,
            "Original sentence.",
            "“当前译文。”",
            [
                {
                    "issue_id": "r1-review-00001",
                    "chapter": 0,
                    "index": 1,
                    "type": "added",
                    "detail": "原文没有对话引号",
                    "suggestion": "删除多余引号",
                }
            ],
        )

        self.assertEqual(patch.after, "修订后的完整译文。")
