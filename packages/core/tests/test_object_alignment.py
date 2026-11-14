"""Protected-object transport and location contracts do not require a document format."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from wenyi_core.agents.annotation_aligner import AnnotationAligner
from wenyi_core.alignment import AlignmentUnit, coincident_point_order, unresolved_alignment
from wenyi_core.alignment.objects import (
    ObjectAnchor,
    clean_object_markers,
    marked_sources,
    object_marker,
    resolve_objects,
)
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient


def anchors():
    return (ObjectAnchor("a", "〘object:a〙", 1), ObjectAnchor("b", "〘object:b〙", 1))


@pytest.mark.parametrize(
    "broken",
    [
        "〘object:a",
        "〘object:a正文",
        "〘object:a\n正文",
        "〘object:a 正文〙",
        "〘object:a〘object:broken",
    ],
)
def test_malformed_transport_is_lossless_at_every_slice_boundary(broken):
    target = f"甲{broken}为正数，另令〘object:b〙为负数。"
    for boundary in range(len(target) + 1):
        slices = (target[:boundary], target[boundary:])
        result = resolve_objects(slices, anchors(), "A B")
        assert result.damaged
        assert result.targets == slices
        assert result.placements == ()
        assert clean_object_markers(slices, "A B") == slices


def test_complete_tokens_crossing_slice_boundaries_are_consumed_once():
    target = "👩‍👧〘object:b〙〘object:a〙cafe\u0301。"
    for boundary in range(len(target) + 1):
        slices = (target[:boundary], target[boundary:])
        result = resolve_objects(slices, anchors(), "A B")
        assert not result.damaged
        assert "".join(result.targets) == "👩‍👧cafe\u0301。"
        assert [row["target_start"] for row in result.placements] == [len("👩‍👧")] * 2
        assert [row["target_order"] for row in result.placements] == [1, 0]
        assert coincident_point_order(result.placements) == (1, 0)


@pytest.mark.parametrize("bad_marker", ["", "〘object:a〙〘object:a〙", "〘object:changed〙"])
def test_missing_duplicate_and_changed_complete_tokens_need_only_missing_object_alignment(
    bad_marker,
):
    result = resolve_objects((f"甲{bad_marker}乙〘object:b〙丙",), anchors(), "A B")
    assert not result.damaged
    assert result.targets == ("甲乙丙",)
    assert [row["id"] for row in result.placements] == ["b"]
    assert result.placements[0]["target_start"] == 2


def test_literal_source_tokens_are_not_consumed():
    source = "A〘object:a〙 B"
    marker = object_marker("a", source)
    assert marker == "〘object:a_x〙"
    target = f"字面〘object:a〙以及{marker}。"
    result = resolve_objects((target,), (ObjectAnchor("a", marker, 1),), source)
    assert result.targets == ("字面〘object:a〙以及。",)
    assert result.placements[0]["target_start"] == len("字面〘object:a〙以及")


def test_unchanged_source_uses_exact_source_boundaries_and_order():
    result = resolve_objects(("A B",), anchors(), "A B")
    assert [row["method"] for row in result.placements] == ["unchanged_source"] * 2
    assert [row["target_start"] for row in result.placements] == [1, 1]
    assert [row["target_order"] for row in result.placements] == [0, 1]


def test_coincident_order_preserves_other_boundaries_and_unordered_slots():
    rows = [
        {"target_start": 1, "target_order": 4},
        {"target_start": 1},  # An existing note keeps its stable slot.
        {"target_start": 1, "target_order": 0},
        {"target_start": 3, "target_order": 1},
        {"target_start": 4, "target_order": False},
    ]
    assert coincident_point_order(rows) == (2, 1, 0, 3, 4)
    assert coincident_point_order([{}, {}]) == (0, 1)


@pytest.mark.parametrize(
    "invalid",
    [
        (anchors()[0], anchors()[0]),
        (replace(anchors()[0], source_offset=-1),),
        (replace(anchors()[0], source_offset=True),),
        (replace(anchors()[0], marker="〘object:a text〙"),),
        (replace(anchors()[0], marker="〘object:a"),),
    ],
)
def test_invalid_caller_contract_is_rejected_without_changing_text(invalid):
    with pytest.raises(ValueError):
        resolve_objects(("unchanged",), invalid, "A B")
    with pytest.raises(ValueError, match="context count"):
        marked_sources(["A", "B"], [[]])


@pytest.mark.parametrize("kind", ["equation", "drawing"])
@pytest.mark.parametrize("abstain", [False, True])
def test_generic_aligner_accepts_non_markup_object_references(kind, abstain):
    reference = {"kind": kind, "source": "E = mc²"}
    unit = AlignmentUnit(
        "paragraph",
        "Let be positive.",
        "设为正数。",
        (
            {
                "id": "object",
                "mode": "point",
                "source_start": 4,
                "source_end": 4,
                "object_reference": reference,
            },
        ),
    )

    def handler(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        assert payload["object_reference"] == reference
        assert "relation" not in payload
        if abstain:
            return '{"id":"object","abstain":true}'
        return json.dumps(
            {"id": "object", "search": "设为", "replacement": f"设{payload['markers'][0]}为"}
        )

    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(handler=handler)
    result = AnnotationAligner(client, config).align_unit(unit)
    assert unit.target == "设为正数。"
    assert result.has_unresolved == abstain
    row = result.placements[0]
    assert row["status"] == ("unresolved" if abstain else "aligned")
    assert row["target_start"] == (None if abstain else 1)
    assert row["target_end"] == row["target_start"]
    assert len(client.calls) == 1


def test_unresolved_alignment_does_not_choose_a_format_fallback():
    unit = AlignmentUnit(
        "paragraph",
        "source",
        "target",
        tuple(
            {"id": relation, "mode": "point", "relation": relation}
            for relation in ("noteref", "inline_object", "future_format")
        ),
    )
    result = unresolved_alignment(unit)
    assert result.has_unresolved
    assert all(row["status"] == "unresolved" for row in result.placements)
    assert all(row["target_start"] is None for row in result.placements)
