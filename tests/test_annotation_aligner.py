"""Tests for immutable EPUB annotation marker alignment."""

from __future__ import annotations

import json
import unittest

from trans_novel.agents.annotation_aligner import (
    AnnotationAligner,
    AnnotationAlignmentError,
    AnnotationUnit,
    build_marked_source,
    target_digest,
    validate_marked_target,
)
from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient


def _config() -> Config:
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {
                    "strong": {"model": "strong"},
                    "cheap": {"model": "cheap"},
                },
            },
        }
    )


def _point_unit(*, target: str = "你好世界") -> AnnotationUnit:
    return AnnotationUnit(
        unit_id="tn1_0",
        source="Hello world",
        target=target,
        items=(
            {
                "id": "tn1_0_annotation_0",
                "mode": "point",
                "source_start": 5,
                "source_end": 5,
                "source_text": "",
                "marker_text": "2",
            },
        ),
    )


def _multi_item_unit(*, target: str = "你好世界，再见") -> AnnotationUnit:
    return AnnotationUnit(
        unit_id="tn2_0",
        source="Hello world, goodbye",
        target=target,
        items=(
            {
                "id": "tn2_0_annotation_0",
                "mode": "point",
                "source_start": 5,
                "source_end": 5,
                "source_text": "",
                "marker_text": "1",
            },
            {
                "id": "tn2_0_annotation_1",
                "mode": "point",
                "source_start": 12,
                "source_end": 12,
                "source_text": "",
                "marker_text": "2",
            },
        ),
    )


def _source_with_markers_of(messages: list[dict[str, str]]) -> str:
    request = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
    return request["items"][0]["source_with_markers"]


def _range_unit() -> AnnotationUnit:
    return AnnotationUnit(
        unit_id="tn1_1",
        source="a long tunnel appeared",
        target="一条长长的隧道出现了",
        items=(
            {
                "id": "tn1_1_annotation_0",
                "mode": "range",
                "source_start": 2,
                "source_end": 13,
                "source_text": "long tunnel",
                "marker_text": "〔＊1〕",
            },
        ),
    )


class TestAnnotationMarkerValidation(unittest.TestCase):
    def test_build_marked_source_for_point_and_range(self):
        self.assertEqual(
            build_marked_source(_point_unit()),
            "Hello⟪tn1_0_annotation_0⟫ world",
        )
        self.assertEqual(
            build_marked_source(_range_unit()),
            "a ⟪tn1_1_annotation_0:S⟫long tunnel⟪tn1_1_annotation_0:E⟫ appeared",
        )

    def test_extracts_exact_point_and_range_offsets(self):
        point = validate_marked_target(
            _point_unit(),
            "你好⟪tn1_0_annotation_0⟫世界",
        )
        self.assertEqual((point[0]["target_start"], point[0]["target_end"]), (2, 2))

        range_placements = validate_marked_target(
            _range_unit(),
            "一条⟪tn1_1_annotation_0:S⟫长长的隧道⟪tn1_1_annotation_0:E⟫出现了",
        )
        self.assertEqual(
            (range_placements[0]["target_start"], range_placements[0]["target_end"]),
            (2, 7),
        )

    def test_rejects_any_target_edit(self):
        with self.assertRaisesRegex(AnnotationAlignmentError, "target_was_modified"):
            validate_marked_target(
                _point_unit(),
                "您好⟪tn1_0_annotation_0⟫世界",
            )

    def test_rejects_missing_unknown_and_duplicate_markers(self):
        unit = _point_unit()
        invalid_outputs = (
            "你好世界",
            "你好⟪unknown⟫世界",
            "你⟪tn1_0_annotation_0⟫好⟪tn1_0_annotation_0⟫世界",
        )
        for output in invalid_outputs:
            with self.subTest(output=output), self.assertRaises(AnnotationAlignmentError):
                validate_marked_target(unit, output)

    def test_rejects_crossing_ranges(self):
        unit = AnnotationUnit(
            unit_id="nested",
            source="abcd",
            target="甲乙丙丁",
            items=(
                {"id": "outer", "mode": "range", "source_start": 0, "source_end": 4},
                {"id": "inner", "mode": "range", "source_start": 1, "source_end": 3},
            ),
        )
        crossing = "⟪outer:S⟫甲⟪inner:S⟫乙⟪outer:E⟫丙⟪inner:E⟫丁"
        with self.assertRaisesRegex(AnnotationAlignmentError, "crossing_or_reversed_range"):
            validate_marked_target(unit, crossing)


class TestAnnotationAligner(unittest.TestCase):
    def test_calls_cheap_tier_and_returns_validated_alignment(self):
        unit = _point_unit()
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                {
                    "items": [
                        {
                            "unit_id": unit.unit_id,
                            "marked_target": "你好⟪tn1_0_annotation_0⟫世界",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

        result = AnnotationAligner(client, _config()).align_unit(unit)

        self.assertFalse(result.used_fallback)
        self.assertEqual(result.target_digest, target_digest(unit.target))
        self.assertEqual(result.placements[0]["target_start"], 2)
        self.assertEqual(client.calls[0]["tier"], "cheap")
        self.assertEqual(client.calls[0]["stage"], "AnnotationAligner")
        self.assertTrue(client.calls[0]["json_mode"])

    def test_model_or_json_failure_returns_deterministic_fallback(self):
        unit = _point_unit()
        clients = (
            FakeClient(handler=lambda messages, tier, json_mode: "not json"),
            FakeClient(
                handler=lambda messages, tier, json_mode: json.dumps(
                    {"items": [{"unit_id": unit.unit_id, "marked_target": "译文被修改"}]},
                    ensure_ascii=False,
                )
            ),
        )
        results = [AnnotationAligner(client, _config()).align_unit(unit) for client in clients]

        self.assertEqual(results[0], results[1])
        self.assertTrue(results[0].used_fallback)
        self.assertEqual(results[0].placements[0]["status"], "fallback")
        self.assertEqual(results[0].placements[0]["method"], "proportional_source_offset")
        self.assertEqual(results[0].placements[0]["target_start"], 2)

    def test_range_fallback_is_zero_width_at_paragraph_end(self):
        unit = _range_unit()
        result = AnnotationAligner(
            FakeClient(handler=lambda messages, tier, json_mode: ""),
            _config(),
        ).align_unit(unit)

        placement = result.placements[0]
        self.assertTrue(result.used_fallback)
        self.assertEqual(placement["method"], "paragraph_end")
        self.assertEqual(placement["target_start"], len(unit.target))
        self.assertEqual(placement["target_end"], len(unit.target))

    def test_empty_target_fallback_stays_at_zero(self):
        unit = _point_unit(target="")
        result = AnnotationAligner(
            FakeClient(handler=lambda messages, tier, json_mode: ""),
            _config(),
        ).align_unit(unit)

        self.assertEqual(result.target_digest, target_digest(""))
        self.assertEqual(result.placements[0]["target_start"], 0)
        self.assertEqual(result.placements[0]["target_end"], 0)

    def test_aligns_multiple_units_in_one_call_and_preserves_order(self):
        point = _point_unit()
        range_unit = _range_unit()

        def handler(messages, tier, json_mode):
            request = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
            self.assertEqual(
                [item["unit_id"] for item in request["items"]],
                [point.unit_id, range_unit.unit_id],
            )
            return json.dumps(
                {
                    "items": [
                        {
                            "unit_id": range_unit.unit_id,
                            "marked_target": (
                                "一条⟪tn1_1_annotation_0:S⟫长长的隧道⟪tn1_1_annotation_0:E⟫出现了"
                            ),
                        },
                        {
                            "unit_id": point.unit_id,
                            "marked_target": "你好⟪tn1_0_annotation_0⟫世界",
                        },
                    ]
                },
                ensure_ascii=False,
            )

        client = FakeClient(handler=handler)
        results = AnnotationAligner(client, _config()).align_units([point, range_unit])

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            [result.unit_id for result in results], [point.unit_id, range_unit.unit_id]
        )
        self.assertTrue(all(not result.used_fallback for result in results))
        self.assertEqual(results[0].placements[0]["target_start"], 2)
        self.assertEqual(results[1].placements[0]["target_start"], 2)
        expected_max_tokens = max(
            1024,
            min(8192, (len(point.target) + len(range_unit.target)) * 2 + 2 * 64 + 512),
        )
        self.assertEqual(client.calls[0]["max_tokens"], expected_max_tokens)

    def test_bad_batch_items_fall_back_individually(self):
        valid = _point_unit()
        missing = AnnotationUnit(
            unit_id="missing",
            source="A note",
            target="缺失",
            items=({"id": "missing_note", "mode": "point", "source_start": 1, "source_end": 1},),
        )
        duplicate = AnnotationUnit(
            unit_id="duplicate",
            source="B note",
            target="重复",
            items=(
                {
                    "id": "duplicate_note",
                    "mode": "point",
                    "source_start": 1,
                    "source_end": 1,
                },
            ),
        )
        invalid = AnnotationUnit(
            unit_id="invalid",
            source="C note",
            target="不可修改",
            items=({"id": "invalid_note", "mode": "point", "source_start": 1, "source_end": 1},),
        )
        response = {
            "items": [
                {
                    "unit_id": valid.unit_id,
                    "marked_target": "你好⟪tn1_0_annotation_0⟫世界",
                },
                {"unit_id": duplicate.unit_id, "marked_target": "重⟪duplicate_note⟫复"},
                {"unit_id": duplicate.unit_id, "marked_target": "重复⟪duplicate_note⟫"},
                {"unit_id": invalid.unit_id, "marked_target": "已被模型修改"},
                {"unit_id": "unknown", "marked_target": "ignored"},
            ]
        }
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(response, ensure_ascii=False)
        )

        results = AnnotationAligner(client, _config()).align_units(
            [valid, missing, duplicate, invalid]
        )

        self.assertEqual(len(client.calls), 1)
        self.assertFalse(results[0].used_fallback)
        self.assertTrue(all(result.used_fallback for result in results[1:]))

    def test_outer_json_failure_falls_back_whole_batch(self):
        units = [_point_unit(), _range_unit()]
        client = FakeClient(handler=lambda messages, tier, json_mode: "not json")

        results = AnnotationAligner(client, _config()).align_units(units)

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(result.used_fallback for result in results))

    def test_duplicate_input_unit_ids_do_not_call_model(self):
        unit = _point_unit()
        client = FakeClient(handler=lambda messages, tier, json_mode: "should not be called")

        results = AnnotationAligner(client, _config()).align_units([unit, unit])

        self.assertEqual(client.calls, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.used_fallback for result in results))


class TestAnnotationAlignerPerItemFanOut(unittest.TestCase):
    """A block with >1 annotation must not bundle every marker into one call."""

    def test_multi_item_block_issues_one_concurrent_request_per_annotation(self):
        unit = _multi_item_unit()

        def handler(messages, tier, json_mode):
            source_with_markers = _source_with_markers_of(messages)
            if "tn2_0_annotation_0" in source_with_markers:
                marked = "你好⟪tn2_0_annotation_0⟫世界，再见"
            else:
                marked = "你好世界，⟪tn2_0_annotation_1⟫再见"
            return json.dumps(
                {"items": [{"unit_id": "tn2_0", "marked_target": marked}]},
                ensure_ascii=False,
            )

        client = FakeClient(handler=handler)
        result = AnnotationAligner(client, _config()).align_unit(unit)

        # One independent request per annotation, not one request for the
        # whole paragraph asking the model to place both markers at once.
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(result.used_fallback)
        self.assertEqual(
            [placement["id"] for placement in result.placements],
            ["tn2_0_annotation_0", "tn2_0_annotation_1"],
        )
        placements = {placement["id"]: placement for placement in result.placements}
        self.assertEqual(placements["tn2_0_annotation_0"]["target_start"], 2)
        self.assertEqual(placements["tn2_0_annotation_1"]["target_start"], 5)

    def test_one_bad_annotation_response_does_not_sink_its_siblings(self):
        """Splitting per item means a single mistake only costs that one note."""
        unit = _multi_item_unit()

        def handler(messages, tier, json_mode):
            source_with_markers = _source_with_markers_of(messages)
            if "tn2_0_annotation_0" in source_with_markers:
                marked = "你好⟪tn2_0_annotation_0⟫世界，再见"
                return json.dumps(
                    {"items": [{"unit_id": "tn2_0", "marked_target": marked}]},
                    ensure_ascii=False,
                )
            # Model drops the Chinese comma while copying the target: this
            # single mistake must only cost annotation_1, not annotation_0.
            marked = "你好世界再见⟪tn2_0_annotation_1⟫"
            return json.dumps(
                {"items": [{"unit_id": "tn2_0", "marked_target": marked}]},
                ensure_ascii=False,
            )

        client = FakeClient(handler=handler)
        result = AnnotationAligner(client, _config()).align_unit(unit)

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(result.used_fallback)
        placements = {placement["id"]: placement for placement in result.placements}
        self.assertEqual(placements["tn2_0_annotation_0"]["status"], "aligned")
        self.assertEqual(placements["tn2_0_annotation_0"]["target_start"], 2)
        self.assertEqual(placements["tn2_0_annotation_1"]["status"], "fallback")

    def test_duplicate_ids_within_one_block_fall_back_without_calling_model(self):
        unit = AnnotationUnit(
            unit_id="tn3_0",
            source="Hello world, goodbye",
            target="你好世界，再见",
            items=(
                {"id": "dup", "mode": "point", "source_start": 5, "source_end": 5},
                {"id": "dup", "mode": "point", "source_start": 12, "source_end": 12},
            ),
        )
        client = FakeClient(handler=lambda messages, tier, json_mode: "should not be called")

        result = AnnotationAligner(client, _config()).align_unit(unit)

        self.assertEqual(client.calls, [])
        self.assertTrue(result.used_fallback)


if __name__ == "__main__":
    unittest.main()
