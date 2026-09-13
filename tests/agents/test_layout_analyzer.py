from __future__ import annotations

import json
import unittest

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.agents.layout_analyzer import LayoutAnalyzer
from trans_novel.config import Config
from trans_novel.llm import FakeClient


class TestLayoutAnalyzer(unittest.TestCase):
    @staticmethod
    def _config() -> Config:
        return Config.from_dict({"llm": fake_llm_dict()})

    def test_sends_original_samples_and_requires_exact_host_ids(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: json.dumps(
                {
                    "observations": [
                        {"node_id": "b", "role": None, "level": None},
                        {"node_id": "a", "role": "quote", "level": None},
                    ]
                }
            )
        )
        analyzer = LayoutAnalyzer(client, self._config())
        result = analyzer.classify(
            samples=[
                {"node_id": "a", "source_markup": "<p>Original</p>"},
                {"node_id": "b", "matched_css": ["p{margin:1em}"]},
            ],
            allowed_roles=("quote",),
        )

        self.assertEqual([item.node_id for item in result], ["a", "b"])
        request = json.loads(client.calls[0]["messages"][-1]["content"])
        self.assertEqual(request["samples"][0]["source_markup"], "<p>Original</p>")
        self.assertEqual(client.calls[0]["agent"], "analyst")
        self.assertEqual(client.calls[0]["operation"], "layout.classify")

    def test_retries_malicious_duplicate_missing_unknown_and_bad_level_responses(self):
        invalid = [
            {"observations": [{"node_id": "evil", "role": "body", "level": None}]},
            {
                "observations": [
                    {"node_id": "a", "role": "body", "level": None},
                    {"node_id": "a", "role": "body", "level": None},
                ]
            },
            {"observations": []},
            {"observations": [{"node_id": "a", "role": "admin", "level": None}]},
            {"observations": [{"node_id": "a", "role": "heading", "level": None}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                client = FakeClient(
                    handler=lambda messages, agent, operation, json_mode, payload=payload: (
                        json.dumps(payload)
                    )
                )
                with self.assertRaises(WorkflowProtocolError):
                    LayoutAnalyzer(client, self._config()).classify(
                        samples=[{"node_id": "a"}], allowed_roles=("body", "heading")
                    )
                self.assertEqual(
                    len(client.calls), self._config().pipeline.protocol_retry_limit + 1
                )


if __name__ == "__main__":
    unittest.main()
