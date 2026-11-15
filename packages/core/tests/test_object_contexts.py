"""Shared read-only object references across formats and model workflows."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from unittest.mock import patch

import pytest
from wenyi_core.agents import prompts
from wenyi_core.agents.precision import PrecisionAgent, PrecisionInputs
from wenyi_core.alignment.objects import marked_sources, object_marker
from wenyi_core.ingest.epub_contexts import _build_epub_annotation_contexts
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.markup.contexts import (
    annotation_contexts_for_segments,
    object_contexts_for_segments,
    reference_source,
)
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.pipeline.translate.translation_batch import BatchPlan, TranslationBatchExecutor
from wenyi_core.review.evidence import BookEvidenceIndex
from wenyi_core.review.run_store import ReviewRunStore

from tests.review_fixtures import autofix_config, autofix_store, fix_json, review_json


def epub_objects():
    html = (
        "<html><body><p>Let <math><msup><mi>x</mi><mn>2</mn></msup></math>"
        " be positive.</p></body></html>"
    )
    _, segments, template = annotate_epub_resource(html, 0, "chapter.xhtml")
    segments[0].target = "设为正数。"
    resource = {"href": "chapter.xhtml", "segments": segments, "template": template}
    registry = _build_epub_annotation_contexts([resource], [resource])
    return Chapter(index=0, segments=segments), registry


def test_epub_review_uses_the_same_readonly_references_as_translation():
    chapter, registry = epub_objects()
    original = deepcopy(chapter)
    expected = annotation_contexts_for_segments(chapter.text_segments, registry)
    evidence = BookEvidenceIndex([chapter], [], {}, annotation_context_registry=registry)
    assert evidence.object_contexts(0, 0) == expected
    assert "<msup>" in expected[0][0]["source"]
    assert chapter == original
    reference = evidence.segment_ref(0, 0)
    assert reference is not None
    assert reference.source == "Let be positive."
    assert reference.target == "设为正数。"


@pytest.mark.parametrize("metadata_key", ["annotations", "epub_annotations", "pdf_annotations"])
def test_reference_contract_assigns_objects_without_format_flags(metadata_key):
    items = [
        {
            "id": f"object-{offset}",
            "relation": "inline_object",
            "target_key": f"object-{offset}",
            "marker": object_marker(f"object-{offset}", "abcdefg"),
            "mode": "point",
            "source_start": offset,
            "source_end": offset,
            "object_source": f"reference-{offset}",
        }
        for offset in (0, 3, 5, 7)
    ]
    items.extend(
        [
            dict(items[1]),
            {
                "id": "note",
                "relation": "noteref",
                "target_key": "note",
                "mode": "range",
                "source_start": 2,
                "source_end": 6,
            },
        ]
    )
    segments = [
        Segment(
            index=0,
            source="abc",
            meta={metadata_key: {"source_length": 7, "items": items}},
        ),
        Segment(index=1, source="defg", cont=True),
    ]
    registry = {
        "contexts": {
            "object-3": {"source_blocks": ["registered content"]},
            "note": {"source_blocks": ["An ordinary note"]},
        }
    }
    original = deepcopy((segments, registry))
    contexts = annotation_contexts_for_segments(segments, registry)
    objects = object_contexts_for_segments(segments, registry)
    assert [[row["target_key"] for row in rows] for rows in objects] == [
        ["object-0", "object-3"],
        ["object-5", "object-7"],
    ]
    assert [[row["source_offset"] for row in rows] for rows in objects] == [["0", "3"], ["2", "4"]]
    assert objects[0][1]["source"] == "registered content"
    assert all(rows[-1]["source"] == "An ordinary note" for rows in contexts)
    assert marked_sources([segment.source for segment in segments], contexts) == [
        f"{items[0]['marker']}abc{items[1]['marker']}",
        f"de{items[2]['marker']}fg{items[3]['marker']}",
    ]
    evidence = BookEvidenceIndex(
        [Chapter(index=4, segments=segments)], [], {}, annotation_context_registry=registry
    )
    assert evidence.object_contexts(4, 0, 2) == objects
    returned = evidence.object_contexts(4, 0, 2)
    returned[0][0]["source"] = "Caller mutation"
    assert evidence.object_contexts(4, 0, 2) == objects
    assert (segments, registry) == original


@pytest.mark.parametrize("embedded", [None, "", " ", ["invalid"], "x²"])
def test_reference_resolution_preserves_transport_without_reference_text(embedded):
    item = {
        "id": "formula",
        "relation": "inline_object",
        "target_key": "formula",
        "marker": object_marker("formula", "Let be positive."),
        "mode": "point",
        "source_start": 4,
        "source_end": 4,
        "object_source": embedded,
    }
    registry = {"contexts": {"formula": {"source_blocks": [" ", None]}}}
    segment = Segment(
        index=0,
        source="Let be positive.",
        meta={"annotations": {"source_length": 16, "items": [item]}},
    )
    references = object_contexts_for_segments([segment], registry)
    assert references[0][0]["source"] == (
        embedded if embedded == "x²" else "Original immutable object"
    )
    assert references[0][0]["marker"] == item["marker"]
    assert reference_source({**item, "relation": "noteref"}, registry) == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"source_start": -1},
        {"source_start": True},
        {"source_end": 99},
        {"source_end": "4"},
        {"marker": None},
        {"target_key": []},
    ],
)
def test_invalid_references_do_not_enter_model_context(changes):
    chapter, registry = epub_objects()
    chapter.segments[0].meta["epub_annotations"]["items"][0].update(changes)
    assert object_contexts_for_segments(chapter.text_segments, registry) == [[]]


def test_format_neutral_metadata_reaches_translation_polish_and_precision(tmp_path):
    chapter, registry = epub_objects()
    segment = chapter.segments[0]
    segment.meta["annotations"] = segment.meta.pop("epub_annotations")
    contexts = annotation_contexts_for_segments([segment], registry)
    marker = contexts[0][0]["marker"]
    target = f"设{marker}为正数。"

    def handler(messages, *_):
        request = "\n".join(message["content"] for message in messages)
        assert "<msup>" in request and marker in request
        assert prompts.protected_object_guidance([marker]).strip() in request
        return json.dumps({"polished" if len(messages) == 4 else "translations": [target]})

    config = autofix_config(str(tmp_path / "state"))
    runtime = PipelineRuntime(config, FakeClient(handler=handler))
    plan = BatchPlan.capture(0, 0, [segment], [], "", "", "", "", contexts, "")
    result = TranslationBatchExecutor(runtime.translator, runtime.polisher).execute(
        plan, polish=True
    )
    assert result.targets == (target,)
    packet = PrecisionAgent(runtime.client, config)._packet(
        PrecisionInputs((segment.source,), (), "", "", "", "", contexts, "")
    )
    assert "<msup>" in packet and marker in packet
    review_prompt = prompts.readonly_object_context(contexts)
    assert prompts.protected_object_guidance([marker]).strip() in review_prompt
    assert "Do not introduce a marker absent from the current translation" in review_prompt
    assert "superscripts" not in review_prompt
    assert "unverified" not in review_prompt


def test_review_workflow_loads_epub_object_registry(tmp_path):
    chapter, registry = epub_objects()
    store = autofix_store(str(tmp_path))
    store.save_chapter(chapter)
    store.save_annotation_contexts(registry)
    config = autofix_config(str(tmp_path / "state"))
    config.pipeline.review_agent_loop = False
    config.pipeline.review_fix_loop = False
    config.pipeline.review_conflict_arbitration = False

    def handler(messages, *_):
        user = messages[-1]["content"]
        assert "<msup>" in user
        assert "untrusted read-only data" in user
        return review_json(user, [])

    client = FakeClient(handler=handler)
    outcome = Orchestrator(config, client)._review.run_session(store, [])
    assert outcome.issues == []
    assert client.calls
    assert store.load_chapter(0) == chapter


@pytest.mark.parametrize("autofix", [False, True])
def test_review_verification_and_fixer_receive_epub_objects(tmp_path, autofix):
    chapter, registry = epub_objects()
    store = autofix_store(str(tmp_path))
    store.save_chapter(chapter)
    store.save_annotation_contexts(registry)
    config = autofix_config(str(tmp_path / "state"))
    config.pipeline.review_agent_loop = True
    config.pipeline.review_fix_loop = not autofix
    config.pipeline.review_clean_confirmations = 1
    config.pipeline.review_conflict_arbitration = False
    # Keep this reference test independent of the format-owned placement policy.
    config.pipeline.annotation_alignment = False
    operations = []

    def handler(messages, *_):
        operation = client.calls[-1]["operation"]
        operations.append(operation)
        user = messages[-1]["content"]
        assert "<msup>" in user
        assert "untrusted read-only data" in user
        if operation == "review.scan":
            issues = (
                []
                if "修订译文。" in user
                else [
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "Missing meaning",
                        "suggestion": "Revise",
                    }
                ]
            )
            return review_json(user, issues)
        if operation in {"review.verify", "autofix.verify"}:
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {"candidate_id": candidate, "verdict": "confirmed"}
                        for candidate in re.findall(r'"candidate_id":\s*"([^"]+)"', user)
                    ],
                    "new_issues": [],
                    "complete": True,
                }
            )
        assert operation in {"review.fix", "autofix.fix"}
        return fix_json(user, "修订译文。")

    client = FakeClient(handler=handler)
    orchestrator = Orchestrator(config, client)
    outcome = orchestrator._review.run_session(store, [])
    assert store.load_chapter(0) == chapter
    if autofix:
        operations.clear()
        orchestrator._review_autofix.run(store, outcome, [])
        assert store.load_chapter(0).text_segments[0].target == "修订译文。"
    else:
        assert outcome.issues == []
    scope = "autofix" if autofix else "review"
    assert f"{scope}.verify" in operations
    assert f"{scope}.fix" in operations


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_review_reuse_binds_readonly_references(tmp_path, interrupted, changed):
    chapter, registry = epub_objects()
    store = autofix_store(str(tmp_path))
    store.save_chapter(chapter)
    store.save_annotation_contexts(registry)
    config = autofix_config(str(tmp_path / "state"))
    config.pipeline.review_agent_loop = False
    config.pipeline.review_fix_loop = False
    config.pipeline.review_conflict_arbitration = False
    client = FakeClient(handler=lambda messages, *_: review_json(messages[-1]["content"], []))
    orchestrator = Orchestrator(config, client)
    save = ReviewRunStore.save_checkpoint

    def checkpoint(debug, value):
        save(debug, value)
        if value["phase"] == "scan_done":
            raise KeyboardInterrupt()

    if interrupted:
        with (
            patch.object(ReviewRunStore, "save_checkpoint", checkpoint),
            pytest.raises(KeyboardInterrupt),
        ):
            orchestrator._review.run_session(store, [])
    else:
        orchestrator._review.run_session(store, [])
    previous = store.load_latest_review_result()
    assert previous is not None
    calls = len(client.calls)
    if changed:
        records = registry["contexts"]
        assert isinstance(records, dict)
        record = next(iter(records.values()))
        record["source_blocks"] = ["<math><mi>y</mi></math>"]
        store.save_annotation_contexts(registry)
    outcome = Orchestrator(config, client)._review.run_session(store, [])
    assert (outcome.result["review_id"] != previous["review_id"]) == changed
    assert (len(client.calls) > calls) == changed
    assert store.load_chapter(0) == chapter


@pytest.mark.parametrize("changed", [False, True])
def test_autofix_reuses_finished_fixer_only_with_identical_object_references(tmp_path, changed):
    chapter, registry = epub_objects()
    store = autofix_store(str(tmp_path))
    store.save_chapter(chapter)
    store.save_annotation_contexts(registry)
    config = autofix_config(str(tmp_path / "state"))
    config.pipeline.review_agent_loop = False
    config.pipeline.review_fix_loop = False
    config.pipeline.review_conflict_arbitration = False
    config.pipeline.annotation_alignment = False
    needle = "<msup>"

    def handler(messages, *_):
        operation = client.calls[-1]["operation"]
        user = messages[-1]["content"]
        assert needle in user
        if operation == "review.scan":
            return review_json(
                user,
                [
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "Missing meaning",
                        "suggestion": "Revise",
                    }
                ],
            )
        if operation == "autofix.verify":
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {"candidate_id": candidate, "verdict": "confirmed"}
                        for candidate in re.findall(r'"candidate_id":\s*"([^"]+)"', user)
                    ],
                    "complete": True,
                }
            )
        assert operation == "autofix.fix"
        return fix_json(user, "修订译文。")

    client = FakeClient(handler=handler)
    orchestrator = Orchestrator(config, client)
    outcome = orchestrator._review.run_session(store, [])
    with (
        patch(
            "wenyi_core.pipeline.autofix.review_autofix.save_plan",
            side_effect=KeyboardInterrupt,
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        orchestrator._review_autofix.run(store, outcome, [])
    assert sum(call["operation"] == "autofix.fix" for call in client.calls) == 1
    assert store.load_chapter(0) == chapter
    if changed:
        records = registry["contexts"]
        assert isinstance(records, dict)
        next(iter(records.values()))["source_blocks"] = ["<math><mi>y</mi></math>"]
        store.save_annotation_contexts(registry)
        needle = "<mi>y</mi>"
    Orchestrator(config, client)._review_autofix.run(store, outcome, [])
    assert sum(call["operation"] == "autofix.fix" for call in client.calls) == 1 + int(changed)
    assert store.load_chapter(0).text_segments[0].target == "修订译文。"
