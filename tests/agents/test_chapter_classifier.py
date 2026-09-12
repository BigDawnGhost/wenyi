from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.agents.chapter_classifier import ChapterClassifier, ChapterObservation
from trans_novel.config import Config
from trans_novel.llm import FakeClient


class TestChapterClassifier(unittest.TestCase):
    @staticmethod
    def _config() -> Config:
        return Config.from_dict({"llm": fake_llm_dict()})

    def test_sends_complete_source_and_advisory_context_in_one_json_call(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: json.dumps(
                {"chapter_id": 7, "kind": "reference_only", "reason": "citations only"}
            )
        )
        classifier = ChapterClassifier(client, self._config(), src="en", tgt="zh")

        result = classifier.classify(
            chapter_id=7,
            title="Misleading title",
            context="structural context",
            source="Alpha.\n\nOmega.",
            hints=["xhtml:section:type=bibliography"],
        )

        self.assertEqual(result.kind, "reference_only")
        self.assertEqual(client.calls[0]["agent"], "analyst")
        self.assertEqual(client.calls[0]["operation"], "chapter.classify")
        self.assertTrue(client.calls[0]["json_mode"])
        request = json.loads(client.calls[0]["messages"][-1]["content"])
        self.assertEqual(request["source"], "Alpha.\n\nOmega.")
        self.assertEqual(request["title_context"], "Misleading title")
        self.assertEqual(request["semantic_hints"], ["xhtml:section:type=bibliography"])

    def test_retries_invalid_protocol_and_requires_exact_integer_id(self):
        responses = [
            {"chapter_id": True, "kind": "reference_only", "reason": "invalid ID"},
            {"chapter_id": 2, "kind": "translatable", "reason": "narrative source"},
        ]
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: json.dumps(responses.pop(0))
        )
        classifier = ChapterClassifier(client, self._config())

        result = classifier.classify(
            chapter_id=2, title="", context="", source="Narrative.", hints=[]
        )

        self.assertEqual(result.kind, "translatable")
        self.assertEqual(len(client.calls), 2)

    def test_mismatched_id_fails_after_bounded_retries(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: json.dumps(
                {"chapter_id": 9, "kind": "uncertain", "reason": "ambiguous"}
            )
        )
        classifier = ChapterClassifier(client, self._config())

        with self.assertRaisesRegex(WorkflowProtocolError, "chapter_classification_id_mismatch"):
            classifier.classify(chapter_id=3, title="", context="", source="Source.", hints=[])
        self.assertEqual(len(client.calls), self._config().pipeline.protocol_retry_limit + 1)

    def test_observation_forbids_coercion_extra_fields_and_empty_reason(self):
        invalid = [
            {"chapter_id": "1", "kind": "translatable", "reason": "valid"},
            {"chapter_id": 1, "kind": "translatable", "reason": " "},
            {"chapter_id": 1, "kind": "translatable", "reason": "valid", "extra": 1},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ChapterObservation.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
