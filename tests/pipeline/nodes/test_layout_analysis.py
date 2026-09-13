from __future__ import annotations

import unittest

from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.agents.layout_analyzer import LayoutObservation
from trans_novel.epub.layout import LAYOUT_POLICY_VERSION, LayoutInventory, LayoutNode
from trans_novel.pipeline.nodes.layout_analysis import analyze_layout


class _Analyzer:
    def __init__(self, decide, *, fail_at=None):
        self.decide = decide
        self.fail_at = fail_at
        self.calls = []

    def classify(self, *, samples, allowed_roles):
        self.calls.append(samples)
        if self.fail_at == len(self.calls):
            raise RuntimeError("interrupted")
        return [
            LayoutObservation(node_id=sample["node_id"], **self.decide(sample))
            for sample in samples
        ]


def _node(index, *, group="g", source_markup=None, extra=None):
    sample = {
        "source_markup": source_markup or f"<p>source {index}</p>",
        "adjacent": {"before": f"before {index}", "after": f"after {index}"},
    }
    sample.update(extra or {})
    return LayoutNode(
        node_id=f"{index:064x}",
        resource_href=f"r{index // 4}.xhtml",
        path=(index,),
        source_sha256=f"{index + 1:064x}",
        group_key=group,
        sample=sample,
    )


def _inventory(nodes):
    return LayoutInventory(
        source_sha256="a" * 64,
        nodes=tuple(nodes),
        digest="b" * 64,
        policy_version=LAYOUT_POLICY_VERSION,
    )


class TestLayoutAnalysis(unittest.TestCase):
    def test_conflicting_same_group_falls_back_to_each_remaining_instance(self):
        nodes = [_node(index) for index in range(9)]
        analyzer = _Analyzer(
            lambda sample: {
                "role": "quote" if "source 0" in sample["source_markup"] else "chapter-summary",
                "level": None,
            }
        )
        checkpoints = []
        profile = analyze_layout(
            _inventory(nodes), analyzer, checkpoint=None, save_checkpoint=checkpoints.append
        )

        self.assertEqual(profile.assignments[0].role, "quote")
        self.assertTrue(all(item.role == "chapter-summary" for item in profile.assignments[1:]))
        requested = [sample["node_id"] for call in analyzer.calls for sample in call]
        self.assertEqual(set(requested), {node.node_id for node in nodes})
        self.assertTrue(checkpoints)

    def test_consistent_representatives_and_held_out_samples_propagate_with_provenance(self):
        nodes = [_node(index) for index in range(9)]
        profile = analyze_layout(
            _inventory(nodes),
            _Analyzer(lambda sample: {"role": "body", "level": None}),
            checkpoint=None,
            save_checkpoint=lambda value: None,
        )

        self.assertEqual(len(profile.provenance["observed_node_ids"]), 6)
        self.assertEqual(len(profile.provenance["propagated_node_ids"]), 3)
        self.assertEqual(profile.provenance["unknown_count"], 0)
        self.assertTrue(all(item.role == "body" for item in profile.assignments))

    def test_null_sample_never_propagates_and_every_node_gets_an_assignment(self):
        nodes = [_node(index) for index in range(7)]
        analyzer = _Analyzer(lambda sample: {"role": None, "level": None})
        profile = analyze_layout(
            _inventory(nodes), analyzer, checkpoint=None, save_checkpoint=lambda value: None
        )
        self.assertEqual(len(profile.assignments), len(nodes))
        self.assertTrue(all(item.role is None for item in profile.assignments))
        self.assertEqual(
            {sample["node_id"] for call in analyzer.calls for sample in call},
            {node.node_id for node in nodes},
        )
        self.assertEqual(profile.provenance["unknown_count"], len(nodes))

    def test_interrupted_run_reuses_every_successful_batch(self):
        nodes = [_node(0, group="a"), _node(1, group="b")]
        inventory = _inventory(nodes)
        checkpoints = []
        interrupted = _Analyzer(lambda sample: {"role": "body", "level": None}, fail_at=2)
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            analyze_layout(
                inventory,
                interrupted,
                checkpoint=None,
                save_checkpoint=checkpoints.append,
            )

        resumed = _Analyzer(lambda sample: {"role": "body", "level": None})
        profile = analyze_layout(
            inventory,
            resumed,
            checkpoint=checkpoints[-1],
            save_checkpoint=checkpoints.append,
        )
        self.assertEqual(len(resumed.calls), 1)
        self.assertEqual(resumed.calls[0][0]["node_id"], nodes[1].node_id)
        self.assertTrue(all(item.role == "body" for item in profile.assignments))

    def test_long_complete_evidence_is_chunked_and_conflict_becomes_unknown(self):
        node = _node(0, source_markup="A" * 17_000)
        analyzer = _Analyzer(
            lambda sample: {
                "role": "quote" if sample["evidence_chunk"]["index"] < 2 else "body",
                "level": None,
            }
        )
        profile = analyze_layout(
            _inventory([node]), analyzer, checkpoint=None, save_checkpoint=lambda value: None
        )
        chunks = [sample for call in analyzer.calls for sample in call]
        self.assertEqual("".join(sample["source_markup"] for sample in chunks), "A" * 17_000)
        self.assertTrue(all(len(sample["source_markup"]) <= 8_000 for sample in chunks))
        self.assertIsNone(profile.assignments[0].role)

    def test_oversized_repeated_context_fails_instead_of_truncating(self):
        node = _node(
            0,
            source_markup="A" * 9_000,
            extra={"matched_css": "B" * 24_000},
        )
        with self.assertRaisesRegex(WorkflowProtocolError, "layout_context_limit"):
            analyze_layout(
                _inventory([node]),
                _Analyzer(lambda sample: {"role": "body", "level": None}),
                checkpoint=None,
                save_checkpoint=lambda value: None,
            )


if __name__ == "__main__":
    unittest.main()
