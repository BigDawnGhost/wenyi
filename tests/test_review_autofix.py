"""Review Autofix 发布阶段测试（离线）。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from trans_novel.config import Config
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import STATUS_DONE, RunStore
from trans_novel.review.run_store import ReviewOutcome, ReviewRunStore


def _config(state_dir: str) -> Config:
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {"strong": {"model": "p"}},
            },
            "pipeline": {
                "review_autofix": True,
                "review_concurrency": 1,
            },
            "output": {"punctuation_normalize": False},
            "paths": {"state_dir": state_dir},
        }
    )


def _store(directory: str, target: str = "正式译文。") -> RunStore:
    store = RunStore(str(Path(directory, "state", "book")))
    store.save_chapter(
        Chapter(
            index=0,
            title="第一章",
            segments=[Segment(index=0, source="原文。", target=target)],
        )
    )
    store.save_manifest(
        {
            "title": "book",
            "source_lang": "ja",
            "target_lang": "zh",
            "source_sha256": "0" * 64,
            "chapters": [{"index": 0, "status": STATUS_DONE}],
        }
    )
    return store


def _outcome(
    store: RunStore,
    *,
    issues: list[dict] | None = None,
    changes: list[dict] | None = None,
) -> ReviewOutcome:
    issues = issues or []
    changes = changes or []
    debug = ReviewRunStore(store.run_dir)
    debug.start(
        reviewed_content_digest="baseline-digest",
        metadata={"config": {}, "glossary_fingerprint": "g"},
    )
    result = debug.finish(
        status="completed",
        termination="max_rounds" if issues else "clean_confirmed",
        summary={
            "issue_count": len(issues),
            "change_count": len(changes),
            "review_round_count": 2,
        },
        issues=issues,
        changes=changes,
    )
    debug.write_json("rounds/final/unresolved_issues.json", issues)
    return ReviewOutcome(run_dir=debug.run_dir, result=result, usage={})


def _agent_final(user: str) -> str:
    candidate_ids = re.findall(r'"candidate_id":\s*"([^"]+)"', user)
    return json.dumps(
        {
            "action": "final",
            "decisions": [
                {
                    "candidate_id": candidate_id,
                    "verdict": "confirmed",
                    "detail": "确认需修订",
                    "suggestion": "补全信息",
                    "reason": "",
                    "consistency": {},
                    "evidence_refs": [],
                }
                for candidate_id in candidate_ids
            ],
            "new_issues": [],
            "complete": True,
        },
        ensure_ascii=False,
    )


def _fix_json(user: str, replacement: str) -> str:
    def field(name: str) -> str:
        match = re.search(rf"^{name}:\s*(.+)$", user, re.MULTILINE)
        if match is None:
            raise AssertionError(f"Fixer prompt missing {name}")
        return match.group(1).strip()

    return json.dumps(
        {
            "segment_ref": field("segment_ref"),
            "before_hash": field("before_hash"),
            "issue_ids": json.loads(field("issue_ids")),
            "replacement": replacement,
            "complete": True,
        },
        ensure_ascii=False,
    )


class TestReviewAutofix(unittest.TestCase):
    def test_export_punctuation_setting_does_not_rewrite_autofix_target(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            outcome = _outcome(
                store,
                changes=[
                    {
                        "chapter": 0,
                        "index": 0,
                        "suggested_target": "change,译文?",
                        "issue_keys": ["issue-1"],
                        "review_result": "not_rereported",
                    }
                ],
            )
            config = _config(str(Path(directory, "state")))
            config.output.punctuation_normalize = True

            fixed = Orchestrator(config, client=FakeClient())._review_autofix.run(
                store, outcome, []
            )

            self.assertEqual(store.load_chapter(0).text_segments[0].target, "change,译文?")
            index = json.loads(
                Path(outcome.run_dir, "autofix", "index.json").read_text(encoding="utf-8")
            )
            records = index["records"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["origin"], "change")
            self.assertEqual(fixed.result["autofix"]["applied_segment_count"], 1)

    def test_changes_are_written_without_adding_segment_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            outcome = _outcome(
                store,
                changes=[
                    {
                        "chapter": 0,
                        "index": 0,
                        "suggested_target": "change 译文。",
                        "issue_keys": ["issue-1"],
                        "review_result": "not_rereported",
                    }
                ],
            )
            orch = Orchestrator(_config(str(Path(directory, "state"))), client=FakeClient())
            annotation_align = Mock()
            style_align = Mock()
            orch._review_autofix._annotations.align_annotations_after_batch = annotation_align
            orch._review_autofix._docx_styles.align_styles_after_batch = style_align

            fixed = orch._review_autofix.run(store, outcome, [])

            self.assertEqual(store.load_chapter(0).text_segments[0].target, "change 译文。")
            raw = json.loads(Path(store.chapter_path(0)).read_text(encoding="utf-8"))
            segment = raw["segments"][0]
            self.assertNotIn("target_before_review", segment)
            self.assertNotIn("review_autofix", segment["meta"])
            index = json.loads(
                Path(outcome.run_dir, "autofix", "index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(index["status"], "completed")
            self.assertEqual(index["records"][0]["before"], "正式译文。")
            self.assertEqual(index["records"][0]["after"], "change 译文。")
            self.assertEqual(index["records"][0]["source_change"]["issue_keys"], ["issue-1"])
            self.assertEqual(fixed.result["autofix"]["applied_segment_count"], 1)
            annotation_align.assert_called_once()
            style_align.assert_called_once()

    def test_final_issues_reuse_agent_loop_after_changes_then_use_fixer(self):
        seen_agent_users: list[str] = []
        seen_fixer_users: list[str] = []

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "取证审校 Agent" in system:
                seen_agent_users.append(user)
                return _agent_final(user)
            if "谨慎修订编辑" in system:
                seen_fixer_users.append(user)
                return _fix_json(user, "Agent 终局译文。")
            return "{}"

        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            issue = {
                "issue_id": "review-00001",
                "issue_key": "issue-1",
                "chapter": 0,
                "index": 0,
                "type": "mistranslation",
                "detail": "信息不完整",
                "suggestion": "补全信息",
                "evidence_refs": [],
            }
            outcome = _outcome(
                store,
                issues=[issue],
                changes=[
                    {
                        "chapter": 0,
                        "index": 0,
                        "suggested_target": "change 工作译文。",
                        "issue_keys": ["older-issue"],
                        "review_result": "needs_revision",
                    }
                ],
            )
            orch = Orchestrator(
                _config(str(Path(directory, "state"))),
                client=FakeClient(handler=handler),
            )

            fixed = orch._review_autofix.run(store, outcome, [])

            self.assertEqual(store.load_chapter(0).text_segments[0].target, "Agent 终局译文。")
            self.assertEqual(len(seen_agent_users), 1)
            self.assertIn("change 工作译文。", seen_agent_users[0])
            self.assertEqual(len(seen_fixer_users), 1)
            self.assertIn("当前完整中文译文】\nchange 工作译文。", seen_fixer_users[0])
            index = json.loads(
                Path(outcome.run_dir, "autofix", "index.json").read_text(encoding="utf-8")
            )
            applied = [record for record in index["records"] if record["status"] == "applied"]
            self.assertEqual(
                [record["origin"] for record in applied], ["change", "final_issue_fix"]
            )
            self.assertEqual(applied[0]["before"], "正式译文。")
            self.assertEqual(applied[0]["after"], "change 工作译文。")
            self.assertEqual(applied[1]["before"], "change 工作译文。")
            self.assertEqual(applied[1]["after"], "Agent 终局译文。")
            self.assertEqual(applied[1]["related_issues"][0]["detail"], "确认需修订")
            self.assertEqual(
                applied[1]["artifacts"],
                [
                    "agents/r3-chunk-ch0-base0-n1.json",
                    "autofix/fixers/ch0-text0.json",
                ],
            )
            self.assertEqual(fixed.result["autofix"]["applied_issue_fix_count"], 1)

    def test_failed_final_fixer_keeps_applied_change_and_records_issue(self):
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            if "取证审校 Agent" in system:
                return _agent_final(messages[-1]["content"])
            if "谨慎修订编辑" in system:
                return "{"
            return "{}"

        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            issue = {
                "issue_id": "review-00001",
                "issue_key": "issue-1",
                "chapter": 0,
                "index": 0,
                "type": "mistranslation",
                "detail": "仍有问题",
                "suggestion": "继续修改",
            }
            outcome = _outcome(
                store,
                issues=[issue],
                changes=[
                    {
                        "chapter": 0,
                        "index": 0,
                        "suggested_target": "change 译文。",
                        "issue_keys": [],
                        "review_result": "needs_revision",
                    }
                ],
            )
            orch = Orchestrator(
                _config(str(Path(directory, "state"))),
                client=FakeClient(handler=handler),
            )

            fixed = orch._review_autofix.run(store, outcome, [])

            self.assertEqual(store.load_chapter(0).text_segments[0].target, "change 译文。")
            self.assertEqual(fixed.result["autofix"]["status"], "partial")
            self.assertEqual(fixed.result["autofix"]["failed_issue_count"], 1)

    def test_pending_index_resumes_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            outcome = _outcome(
                store,
                changes=[
                    {
                        "chapter": 0,
                        "index": 0,
                        "suggested_target": "可恢复译文。",
                        "issue_keys": [],
                        "review_result": "not_rereported",
                    }
                ],
            )
            cfg = _config(str(Path(directory, "state")))
            first = Orchestrator(cfg, client=FakeClient())
            first._review_autofix.run(store, outcome, [])

            debug = ReviewRunStore.open_existing(outcome.run_dir)
            index = debug.load_json("autofix/index.json")
            assert isinstance(index, dict)
            index["status"] = "applying"
            index["records"][0]["status"] = "planned"
            index["locations"][0]["status"] = "pending"
            index["locations"][0]["alignment_status"] = "pending"
            debug.write_json("autofix/index.json", index)
            result = debug.load_json("result.json")
            assert isinstance(result, dict)
            result.pop("autofix", None)
            debug.write_json("result.json", result)
            chapter = store.load_chapter(0)
            chapter.text_segments[0].target = "正式译文。"
            store.save_chapter(chapter)

            client = FakeClient(handler=lambda *_args, **_kwargs: "model must not run")
            resumed = Orchestrator(cfg, client=client)._review_autofix.resume_pending(store)

            self.assertIsNotNone(resumed)
            self.assertEqual(client.calls, [])
            self.assertEqual(store.load_chapter(0).text_segments[0].target, "可恢复译文。")
            saved_index = debug.load_json("autofix/index.json")
            assert isinstance(saved_index, dict)
            self.assertEqual(saved_index["status"], "completed")

    def test_newer_review_prevents_publishing_an_older_pending_index(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            older = _outcome(
                store,
                changes=[
                    {
                        "chapter": 0,
                        "index": 0,
                        "suggested_target": "旧 Review 候选。",
                        "issue_keys": [],
                        "review_result": "not_rereported",
                    }
                ],
            )
            cfg = _config(str(Path(directory, "state")))
            service = Orchestrator(cfg, client=FakeClient())._review_autofix
            service.run(store, older, [])

            older_debug = ReviewRunStore.open_existing(older.run_dir)
            index = older_debug.load_json("autofix/index.json")
            assert isinstance(index, dict)
            index["status"] = "applying"
            index["locations"][0]["status"] = "pending"
            older_debug.write_json("autofix/index.json", index)
            chapter = store.load_chapter(0)
            chapter.text_segments[0].target = "正式译文。"
            store.save_chapter(chapter)

            newer = _outcome(store)
            self.assertGreater(Path(newer.run_dir).name, Path(older.run_dir).name)

            resumed = service.resume_pending(store)

            self.assertIsNone(resumed)
            self.assertEqual(store.load_chapter(0).text_segments[0].target, "正式译文。")


if __name__ == "__main__":
    unittest.main()
