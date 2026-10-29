"""Offline arbitration prompt contracts through the whole-book Review workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.i18n.prompts import render
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.runstore import STATUS_DONE
from wenyi_core.review.evidence import BookEvidenceIndex

from tests.review_fixtures import _chapter, _config, autofix_store, review_json


@pytest.mark.parametrize("target", ["zh", "en", "ru"])
def test_arbiter_prompt_explains_literal_citations_and_complete_final_json(target):
    system = render("review_arbiter_system", src="en", tgt=target, max_evidence_rounds=2)

    assert "exact, unmodified identifier" in system
    assert "samples[].segment_ref" in system
    assert "ref field in successful evidence tool results" in system
    assert "A request_id names a tool request, not a citable evidence item." in system
    assert "use [] instead of inventing one" in system
    assert (
        "all seven keys: action, conflict_id, status, recommended_value, reason, "
        "evidence_refs, complete"
    ) in system
    assert "Never omit complete." in system
    assert "complete:true (JSON boolean true, not a string)" in system
    assert "For an evidence request, always set complete:false." in system
    assert "Use status=unresolved when evidence is insufficient." in system


def _run_conflict(tmp_path, proposals=("安", "安妮"), *, citation="sample", completion=True):
    config = _config()
    config.pipeline.review_agent_loop = True
    config.pipeline.review_conflict_arbitration = True
    config.pipeline.review_fix_loop = False
    config.pipeline.review_autofix = False
    config.pipeline.review_concurrency = 1
    store = autofix_store(str(tmp_path))
    for chapter, proposed in enumerate(proposals):
        store.save_chapter(_chapter(chapter, [(f"Ann in scene {chapter}.", f"{proposed}。")]))
    manifest = store.load_manifest()
    manifest["source_lang"] = "en"
    manifest["chapters"] = [{"index": index, "status": STATUS_DONE} for index in range(2)]
    store.save_manifest(manifest)
    before = [Path(store.chapter_path(index)).read_bytes() for index in range(2)]

    def handler(messages, tier, json_mode):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "translation reviewer" in system:
            return review_json(
                user,
                [
                    {
                        "index": 0,
                        "type": "terminology",
                        "detail": "Check name",
                        "suggestion": "Unify",
                    }
                ],
            )
        if "final arbiter" not in system:
            chapter = 0 if "Ann in scene 0." in user else 1
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {
                            "candidate_id": candidate_id,
                            "verdict": "confirmed",
                            "detail": "Conflicting name",
                            "suggestion": f"Use {proposals[chapter]}",
                            "consistency": {
                                "kind": "term",
                                "subject_source": "Ann",
                                "proposed_value": proposals[chapter],
                            },
                            "evidence_refs": [],
                        }
                        for candidate_id in re.findall(r'"candidate_id":\s*"([^"]+)"', user)
                    ],
                    "new_issues": [],
                    "complete": True,
                }
            )
        conflict = json.JSONDecoder().raw_decode(messages[1]["content"].split("\n", 1)[1])[0]
        sample = next(
            proposal["samples"][0]
            for proposal in conflict["proposals"]
            if proposal["proposed_value"] == "安"
        )
        if citation in {"tool", "request_id"} and len(messages) == 2:
            return json.dumps(
                {
                    "action": "request_evidence",
                    "requests": [
                        {
                            "request_id": "lookup-ann",
                            "tool": "glossary_term",
                            "arguments": {"term": "Ann"},
                        }
                    ],
                    "complete": False,
                }
            )
        ref = sample["segment_ref"]
        if citation == "tool":
            results = json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]
            ref = next(iter(BookEvidenceIndex.evidence_refs(results)))
        elif citation == "request_id":
            ref = "lookup-ann"
        elif citation == "issue_id":
            ref = sample["issue_id"]
        elif citation == "invented":
            ref = "invented:ref"
        elif citation == "modified":
            ref += " (supports the glossary)"
        response = {
            "action": "final",
            "conflict_id": conflict["conflict_id"],
            "status": "suggested",
            "recommended_value": "安",
            "reason": "Follow the glossary.",
            "evidence_refs": [ref],
        }
        if completion is not None:
            response["complete"] = completion
        return json.dumps(response)

    client = FakeClient(handler=handler)
    outcome = Orchestrator(config, client)._review.run_session(
        store, [GlossaryTerm(source="Ann", target="安", type="person")]
    )
    assert [Path(store.chapter_path(index)).read_bytes() for index in range(2)] == before
    conflicts = store.read_artifact(
        f"reviews/{outcome.result['review_id']}/rounds/001/conflicts.json"
    )
    assert isinstance(conflicts, list) and len(conflicts) == 1
    trace = store.read_artifact(
        f"reviews/{outcome.result['review_id']}/rounds/001/agents/"
        f"arbiter-{conflicts[0]['conflict_id']}.json"
    )
    assert isinstance(trace, dict)
    return conflicts[0]["arbitration"], trace, client


@pytest.mark.parametrize("proposals", [("安", "安妮"), ("安妮", "安")])
@pytest.mark.parametrize("citation", ["sample", "tool"])
def test_review_accepts_exact_sample_and_tool_refs_in_either_proposal_order(
    tmp_path, proposals, citation
):
    result, trace, client = _run_conflict(tmp_path, proposals, citation=citation)

    assert result["status"] == "suggested"
    assert result["recommended_value"] == "安"
    assert result["evidence_refs"]
    assert trace["status"] == "finished"
    assert trace["turns"][-1]["parsed"]["complete"] is True
    assert trace["turns"][-1]["parsed"]["evidence_refs"] == result["evidence_refs"]
    operations = [call["operation"] for call in client.calls]
    assert operations.count("review.scan") == 2
    assert operations.count("review.verify") == 2
    assert operations.count("review.arbitrate") == (2 if citation == "tool" else 1)


@pytest.mark.parametrize("citation", ["request_id", "issue_id", "invented", "modified"])
def test_review_rejects_non_evidence_identifiers_without_sanitizing_them(tmp_path, citation):
    result, trace, _ = _run_conflict(tmp_path, citation=citation)

    assert result["status"] == "unresolved"
    assert "unknown_evidence_ref" in result["reason"]
    assert trace["status"] == "fallback"
    assert trace["fallback_reason"] == "unknown_evidence_ref"
    assert trace["turns"][-1]["parsed"]["evidence_refs"] != result["evidence_refs"]


@pytest.mark.parametrize("completion", [None, False, "true"])
def test_review_rejects_missing_or_non_boolean_completion(tmp_path, completion):
    result, trace, _ = _run_conflict(tmp_path, completion=completion)

    assert result["status"] == "unresolved"
    assert trace["status"] == "fallback"
    assert trace["fallback_reason"] == "final_not_complete"
