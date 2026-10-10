"""Real orchestration and prompt wiring with metered, source-grounded fake responses."""

from __future__ import annotations

import json

import pytest
from wenyi_core.agents.terminology import TerminologyKnowledgeError, TerminologyReferenceError
from wenyi_core.config import Config
from wenyi_core.glossary.store import source_matches_text
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import MeteredFakeClient, routing_handler

NAMES = {"Alice": "艾丽丝", "Beth": "贝丝", "Carl": "卡尔"}


class FamilyHandler:
    def __init__(self, *, interrupt_postcheck=False):
        self.interrupt_postcheck = interrupt_postcheck
        self.translation_inputs = []

    def __call__(self, messages, tier, json_mode):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "pre-translation analyst" in system:
            return json.dumps(
                {
                    "style_guide": "Keep the narrator's restraint.",
                    "characters": [
                        {"source": "Alice", "target": NAMES["Alice"], "note": "LOCAL ALICE"}
                    ],
                    "terms": [],
                }
            )
        if system.startswith("You discover terminology"):
            payload = json.loads(user.split("\n", 1)[1])
            terms = []
            for name, target in NAMES.items():
                refs = [
                    p["reference"]
                    for p in payload["passages"]
                    if source_matches_text(name, p["source"])
                ]
                if refs:
                    terms.append(
                        {"source": name, "target": target, "type": "person", "evidence_refs": refs}
                    )
            return json.dumps({"terms": terms})
        if system.startswith("You extract terminology knowledge"):
            payload = json.loads(user.split("\n", 1)[1])
            return json.dumps(
                {
                    "note": payload["term"]["target"] + "的全书人物说明。",
                    "evidence_refs": [p["reference"] for p in payload["passages"]],
                }
            )
        if "terminology" in system and "extractor" in system:
            if self.interrupt_postcheck:
                self.interrupt_postcheck = False
                raise KeyboardInterrupt("after saved body, before extraction checkpoint")
            return '{"terms":[]}'
        if "literary translator" in system:
            self.translation_inputs.append(user)
        return routing_handler(messages, tier, json_mode)


def inputs(tmp_path, *, enabled=True):
    source = tmp_path / "family.txt"
    source.write_text(
        "# First\n\nAlice waited for Carl, her uncle.\n\n"
        "# Later\n\nBeth was Alice's mother.\n\nCarl was Beth's elder brother.\n",
        encoding="utf-8",
    )
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "book_understanding": False,
                "terminology_context": enabled,
                "review": False,
                "polish": False,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    return source, config, FileStorage(str(tmp_path / "run"))


def test_first_real_translation_receives_admitted_notes(tmp_path):
    source, config, store = inputs(tmp_path)
    handler = FamilyHandler()
    client = MeteredFakeClient(handler=handler)
    Orchestrator(config, client=client, storage=store, allow_terminology_context=True).run(
        str(source)
    )
    operations = [call["operation"] for call in client.calls]
    assert operations.index("terminology.evidence") < operations.index("translation.body")
    assert operations.index("terminology.discover") < operations.index("translation.body")
    first = handler.translation_inputs[0]
    assert "全书人物说明" in first
    assert "LOCAL ALICE" not in first
    assert store.pending_chapters() == []
    assert store.get_term("Alice").note.endswith("全书人物说明。")
    assert store.load_usage()["totals"]["calls"] == client.usage_summary()["totals"]["calls"]


def test_saved_body_resumes_only_missing_postcheck_without_double_usage(tmp_path):
    source, config, store = inputs(tmp_path)
    first_client = MeteredFakeClient(handler=FamilyHandler(interrupt_postcheck=True))
    with pytest.raises(KeyboardInterrupt, match="before extraction"):
        Orchestrator(
            config, client=first_client, storage=store, allow_terminology_context=True
        ).run(str(source))
    first_targets = [s.target for s in store.load_chapter(0).text_segments]
    assert first_targets and all(target is not None for target in first_targets)
    assert store.completed_batch_glossary_keys(0) == set()
    previous_calls = store.load_usage()["totals"]["calls"]
    resumed_handler = FamilyHandler()
    resumed = MeteredFakeClient(handler=resumed_handler)
    Orchestrator(config, client=resumed, storage=store, allow_terminology_context=True).run(
        str(source)
    )
    assert [s.target for s in store.load_chapter(0).text_segments] == first_targets
    assert all("Alice waited for Carl" not in text for text in resumed_handler.translation_inputs)
    assert store.completed_batch_glossary_keys(0)
    assert store.load_usage()["totals"]["calls"] == (
        previous_calls + resumed.usage_summary()["totals"]["calls"]
    )
    assert store.pending_chapters() == []


def test_disabled_feature_makes_no_terminology_calls_or_artifacts(tmp_path):
    source, config, store = inputs(tmp_path, enabled=False)
    client = MeteredFakeClient(handler=FamilyHandler())
    Orchestrator(config, client=client, storage=store, allow_terminology_context=True).run(
        str(source)
    )
    assert not any(call["operation"].startswith("terminology.") for call in client.calls)
    assert store.list_artifacts("terminology/") == []
    assert store.get_term("Alice").note == "LOCAL ALICE"


def test_initialization_retry_reuses_analysis_and_preserves_usage_and_conflicts(tmp_path):
    source, config, store = inputs(tmp_path)
    config.pipeline.book_understanding = True

    class InitializationHandler(FamilyHandler):
        fail = False

        def __call__(self, messages, tier, json_mode):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "pre-translation analyst" in system:
                return json.dumps(
                    {
                        "characters": [{"source": "Alice", "target": NAMES["Alice"]}],
                        "terms": [
                            {"source": "Alice", "target": "不同建议", "type": "person"},
                            {"source": "Carl", "target": NAMES["Carl"], "type": "person"},
                        ],
                    }
                )
            if system.startswith("You extract terminology knowledge"):
                payload = json.loads(user.split("\n", 1)[1])
                if self.fail and payload["term"]["source"] == "Carl":
                    raise RuntimeError("Interrupted second seed")
            return super().__call__(messages, tier, json_mode)

    interrupted = InitializationHandler()
    interrupted.fail = True
    client = MeteredFakeClient(handler=interrupted)
    with pytest.raises(RuntimeError, match="second seed"):
        Orchestrator(config, client=client, storage=store, allow_terminology_context=True).prepare(
            str(source)
        )
    assert not store.exists()
    assert len(store.open_conflicts()) == 1
    previous_calls = store.load_usage()["totals"]["calls"]
    assert previous_calls == client.usage_summary()["totals"]["calls"]
    resumed = MeteredFakeClient(handler=InitializationHandler())
    Orchestrator(config, client=resumed, storage=store, allow_terminology_context=True).prepare(
        str(source)
    )
    operations = [call["operation"] for call in resumed.calls]
    assert operations == ["terminology.evidence"]
    assert store.exists()
    assert store.get_term("Alice").status == "conflict"
    assert len(store.open_conflicts()) == 1
    assert store.get_term("Carl") is not None
    assert store.load_usage()["totals"]["calls"] == previous_calls + 1


@pytest.mark.parametrize("reason", ["missing", "outside_input"])
def test_reference_failure_is_diagnosed_and_resume_reuses_paid_analysis_and_evidence(
    tmp_path, reason
):
    source, config, store = inputs(tmp_path)

    class ReferenceHandler(FamilyHandler):
        fail = True

        def __call__(self, messages, tier, json_mode):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "pre-translation analyst" in system:
                return json.dumps(
                    {
                        "characters": [
                            {"source": "Alice", "target": NAMES["Alice"]},
                            {"source": "Carl", "target": NAMES["Carl"]},
                        ],
                        "terms": [],
                    }
                )
            if self.fail and system.startswith("You extract terminology knowledge"):
                payload = json.loads(user.split("\n", 1)[1])
                if payload["term"]["source"] == "Carl":
                    return json.dumps(
                        {
                            "note": "PRIVATE_FAILED_NOTE",
                            "evidence_refs": [] if reason == "missing" else ["PRIVATE_BAD_REF"],
                        }
                    )
            return super().__call__(messages, tier, json_mode)

    first = MeteredFakeClient(handler=ReferenceHandler())
    with pytest.raises(TerminologyReferenceError) as caught:
        Orchestrator(config, client=first, storage=store, allow_terminology_context=True).prepare(
            str(source)
        )
    assert caught.value.reason == reason
    assert not store.exists()
    assert store.get_term("Alice") is not None
    assert store.get_term("Carl") is None
    diagnostics = store.list_artifacts("terminology/diagnostics/evidence/")
    assert len(diagnostics) == 1
    diagnostic = store.read_artifact(diagnostics[0])
    assert diagnostic["schema_version"] == 1
    assert diagnostic["reason"] == reason
    assert diagnostic["operation"] == "terminology.evidence"
    assert diagnostic["field"] == "evidence_refs"
    assert diagnostic["allowed_count"] > 0
    assert diagnostic["received_count"] == (0 if reason == "missing" else 1)
    assert diagnostic["unknown_count"] == (0 if reason == "missing" else 1)
    assert store.read_artifact(diagnostic["request_key"]) is None
    assert len(store.list_artifacts("terminology/requests/evidence/")) == 1
    events = store.list_events(event_type="terminology_reference_validation_failed")
    assert len(events) == 1
    assert events[0]["diagnostic_key"] == diagnostics[0]
    assert "PRIVATE" not in json.dumps(diagnostic)
    assert "PRIVATE" not in json.dumps(events)
    previous = store.load_usage()["totals"]["calls"]
    assert previous == first.usage_summary()["totals"]["calls"]

    handler = ReferenceHandler()
    handler.fail = False
    resumed = MeteredFakeClient(handler=handler)
    Orchestrator(config, client=resumed, storage=store, allow_terminology_context=True).prepare(
        str(source)
    )
    assert [call["operation"] for call in resumed.calls] == ["terminology.evidence"]
    assert store.exists() and store.get_term("Carl") is not None
    assert store.load_usage()["totals"]["calls"] == previous + 1
    assert len(store.list_artifacts("terminology/requests/evidence/")) == 2


@pytest.mark.parametrize("kind", ["evidence", "merge"])
@pytest.mark.parametrize(
    "invalid_response,field,code",
    [
        ({"reading": {"PRIVATE_KEY": "PRIVATE_VALUE"}}, "reading", "string_type"),
        ({"note": "PRIVATE_" + "x" * 4000}, "note", "string_too_long"),
        ({"reading": "PRIVATE_" + "x" * 256}, "reading", "string_too_long"),
        ({"gender": "PRIVATE_" + "x" * 256}, "gender", "string_too_long"),
    ],
)
def test_schema_failure_retains_successful_requests_and_usage_on_prepare_resume(
    tmp_path, kind, invalid_response, field, code
):
    source, config, store = inputs(tmp_path)
    source.write_text("# First\n\n" + "Alice waited quietly. " * 2600, encoding="utf-8")

    class SchemaHandler(FamilyHandler):
        def __init__(self, *, fail):
            super().__init__()
            self.fail = fail
            self.evidence_calls = 0
            self.merge_calls = 0

        def __call__(self, messages, tier, json_mode):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "pre-translation analyst" in system:
                return json.dumps({"characters": [{"source": "Alice", "target": NAMES["Alice"]}]})
            if system.startswith("You extract terminology knowledge"):
                self.evidence_calls += 1
                payload = json.loads(user.split("\n", 1)[1])
                if self.fail and kind == "evidence" and self.evidence_calls == 2:
                    return json.dumps(invalid_response)
                return json.dumps(
                    {
                        "note": f"Supported group {payload['passages'][0]['source_reference']}.",
                        "evidence_refs": [p["reference"] for p in payload["passages"]],
                    }
                )
            if system.startswith("Merge supplied source-grounded terminology prose"):
                self.merge_calls += 1
                payload = json.loads(user.split("\n", 1)[1])
                if self.fail and kind == "merge" and self.merge_calls == 3:
                    return json.dumps(invalid_response)
                return json.dumps(
                    {
                        "note": " ".join(result["note"] for result in payload["results"]),
                        "evidence_refs": [result["reference"] for result in payload["results"]],
                    }
                )
            return super().__call__(messages, tier, json_mode)

    first = MeteredFakeClient(handler=SchemaHandler(fail=True))
    with pytest.raises(TerminologyKnowledgeError) as caught:
        Orchestrator(config, client=first, storage=store, allow_terminology_context=True).prepare(
            str(source)
        )
    assert caught.value.operation == f"terminology.{kind}"
    assert not store.exists()
    assert store.get_term("Alice") is None
    diagnostics = store.list_artifacts(f"terminology/diagnostics/{kind}/")
    assert len(diagnostics) == 1
    diagnostic = store.read_artifact(diagnostics[0])
    assert diagnostic["reason"] == "invalid_fields"
    assert diagnostic["issues"] == [{"field": field, "code": code}]
    assert store.read_artifact(diagnostic["request_key"]) is None
    events = store.list_events(event_type="terminology_knowledge_validation_failed")
    assert len(events) == 1 and events[0]["diagnostic_key"] == diagnostics[0]
    assert "PRIVATE" not in str(caught.value) + json.dumps(diagnostic) + json.dumps(events)
    saved = {key: store.read_artifact(key) for key in store.list_artifacts("terminology/requests/")}
    assert saved
    if kind == "merge":
        assert len(store.list_artifacts("terminology/requests/merge/")) == 2
    previous = store.load_usage()["totals"]["calls"]
    assert previous == first.usage_summary()["totals"]["calls"]

    resumed = MeteredFakeClient(handler=SchemaHandler(fail=False))
    Orchestrator(config, client=resumed, storage=store, allow_terminology_context=True).prepare(
        str(source)
    )
    assert store.exists() and store.get_term("Alice") is not None
    assert all(store.read_artifact(key) == value for key, value in saved.items())
    assert all(call["operation"] != "analysis.sample" for call in resumed.calls)
    if kind == "merge":
        assert all(call["operation"] == "terminology.merge" for call in resumed.calls)
    assert store.load_usage()["totals"]["calls"] == (
        previous + resumed.usage_summary()["totals"]["calls"]
    )
    after = store.load_usage()
    cached = MeteredFakeClient(handler=SchemaHandler(fail=False))
    Orchestrator(config, client=cached, storage=store, allow_terminology_context=True).prepare(
        str(source)
    )
    assert cached.calls == [] and store.load_usage() == after


def test_deferred_history_cache_resumes_without_rewriting_targets_or_rebilling_alignment(tmp_path):
    source, config, store = inputs(tmp_path)
    config.segment.max_tokens_per_batch = 1

    class DelayedDiscoveryHandler(FamilyHandler):
        def __init__(self, *, interrupt):
            super().__init__()
            self.interrupt = interrupt
            self.alignment_seen = False

        def __call__(self, messages, tier, json_mode):
            system, user = messages[0]["content"], messages[-1]["content"]
            if system.startswith("You are a terminology consistency aligner"):
                self.alignment_seen = True
                return '{"terms":[]}'
            if "literary translator" in system and self.interrupt and self.alignment_seen:
                raise KeyboardInterrupt("after cached historical deferral")
            result = super().__call__(messages, tier, json_mode)
            if system.startswith("You discover terminology"):
                request = json.loads(user.split("\n", 1)[1])
                if any("Alice waited" in p["source"] for p in request["passages"]):
                    data = json.loads(result)
                    data["terms"] = [term for term in data["terms"] if term["source"] != "Carl"]
                    return json.dumps(data)
            return result

    first = MeteredFakeClient(handler=DelayedDiscoveryHandler(interrupt=True))
    with pytest.raises(KeyboardInterrupt, match="cached historical deferral"):
        Orchestrator(config, client=first, storage=store, allow_terminology_context=True).run(
            str(source)
        )
    manifest = store.load_manifest()
    saved = {
        (row["index"], segment.index): segment.target
        for row in manifest["chapters"]
        for segment in store.load_chapter(row["index"]).text_segments
        if segment.target is not None
    }
    assert saved
    assert store.get_term("Carl") is None
    assert store.list_artifacts("glossary/history/requests/")
    events = store.list_events(event_type="glossary_history_alignment_deferred")
    assert len(events) == 1
    assert sum(call["operation"] == "glossary.align_history" for call in first.calls) == 1
    previous = store.load_usage()["totals"]["calls"]
    assert previous == first.usage_summary()["totals"]["calls"]
    resumed = MeteredFakeClient(handler=DelayedDiscoveryHandler(interrupt=False))
    Orchestrator(config, client=resumed, storage=store, allow_terminology_context=True).run(
        str(source)
    )
    assert all(call["operation"] != "glossary.align_history" for call in resumed.calls)
    for (chapter, index), target in saved.items():
        assert (
            next(
                segment.target
                for segment in store.load_chapter(chapter).segments
                if segment.index == index
            )
            == target
        )
    assert store.get_term("Carl") is None
    assert store.pending_chapters() == []
    assert store.list_events(event_type="glossary_history_alignment_deferred") == events
    assert store.load_usage()["totals"]["calls"] == (
        previous + resumed.usage_summary()["totals"]["calls"]
    )
    after = store.load_usage()
    cached = MeteredFakeClient(handler=DelayedDiscoveryHandler(interrupt=False))
    Orchestrator(config, client=cached, storage=store, allow_terminology_context=True).run(
        str(source)
    )
    assert cached.calls == []
    assert store.load_usage() == after


def test_enabling_context_on_an_initialized_run_admits_new_analysis_candidates(tmp_path):
    source, config, store = inputs(tmp_path, enabled=False)
    Orchestrator(config, client=MeteredFakeClient(handler=FamilyHandler()), storage=store).prepare(
        str(source)
    )
    assert store.get_term("Carl") is None
    config.pipeline.terminology_context = True

    class Refreshed(FamilyHandler):
        def __call__(self, messages, tier, json_mode):
            if "pre-translation analyst" in messages[0]["content"]:
                return json.dumps(
                    {"characters": [{"source": "Carl", "target": NAMES["Carl"]}], "terms": []}
                )
            return super().__call__(messages, tier, json_mode)

    Orchestrator(
        config,
        client=MeteredFakeClient(handler=Refreshed()),
        storage=store,
        allow_terminology_context=True,
    ).prepare_for_translation(str(source))
    assert store.get_term("Carl") is not None
    assert store.get_term("Carl").note.endswith("全书人物说明。")
    assert all(
        s.target is None
        for row in store.load_manifest()["chapters"]
        for s in store.load_chapter(row["index"]).text_segments
    )


def test_digest_failure_keeps_paid_usage_and_successful_chapter_cache(tmp_path):
    source, config, store = inputs(tmp_path)
    config.pipeline.book_understanding = True

    class FailedDigest(FamilyHandler):
        def __call__(self, messages, tier, json_mode):
            if "chapter digest writer" in messages[0]["content"]:
                if "Alice waited" in messages[-1]["content"]:
                    return ""
            return super().__call__(messages, tier, json_mode)

    first = MeteredFakeClient(handler=FailedDigest())
    with pytest.raises(ValueError, match="Chapter digests"):
        Orchestrator(config, client=first, storage=store, allow_terminology_context=True).prepare(
            str(source)
        )
    assert not store.exists()
    previous = first.usage_summary()["totals"]["calls"]
    assert previous == 2
    assert store.load_usage()["totals"]["calls"] == previous
    resumed = MeteredFakeClient(handler=FamilyHandler())
    Orchestrator(config, client=resumed, storage=store, allow_terminology_context=True).prepare(
        str(source)
    )
    assert [call["operation"] for call in resumed.calls].count("synopsis.chapter") == 1
    assert store.load_usage()["totals"]["calls"] == (
        previous + resumed.usage_summary()["totals"]["calls"]
    )
