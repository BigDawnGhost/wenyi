"""Minimal pipeline planning and preset contracts (offline)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trans_novel.config import Config, PipelineConfig
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.pipeline import build_workflow_definition
from trans_novel.pipeline.contracts import (
    GOAL_RUN_ALL,
    ExecutionGoal,
    assemble_goal,
    qa_goal,
    report_goal,
)
from trans_novel.pipeline.planning import (
    Planner,
    PrescanInputs,
    WorkflowPolicy,
    build_prescan_inputs,
    fingerprints,
)
from trans_novel.pipeline.state import (
    NODE_ASSEMBLE,
    NODE_DETERMINISTIC_QA,
    NODE_LAYOUT,
    NODE_MINE_TERMS,
    NODE_NAME_TERMS,
    NODE_POLISH,
    NODE_PREPARE,
    NODE_REPAIR,
    NODE_REPORT,
    NODE_TITLES,
    NODE_TRANSLATE,
    IdentityMismatchError,
    RunIdentity,
    RunStore,
)
from trans_novel.pipeline.state.models import TRANSLATION_POLICY_VERSION


class TestPresets(unittest.TestCase):
    def test_exact_quality_contract(self):
        expected = {
            "economy": False,
            "balanced": False,
            "quality": True,
        }
        for name, polish in expected.items():
            policy = PipelineConfig.for_quality(name)
            self.assertEqual(policy.polish, polish)
            self.assertFalse(hasattr(policy, "back_matter"))


class TestWorkflowDefinition(unittest.TestCase):
    def test_only_minimal_nodes_are_registered(self):
        self.assertEqual(
            set(build_workflow_definition().node_ids),
            {
                NODE_PREPARE,
                "analyze",
                NODE_LAYOUT,
                NODE_MINE_TERMS,
                NODE_NAME_TERMS,
                NODE_TRANSLATE,
                NODE_POLISH,
                NODE_TITLES,
                NODE_DETERMINISTIC_QA,
                NODE_REPAIR,
                NODE_REPORT,
                NODE_ASSEMBLE,
            },
        )

    def test_body_chain_is_translate_then_optional_polish(self):
        definition = build_workflow_definition()
        self.assertEqual(definition.depends_on(NODE_POLISH), (NODE_TRANSLATE,))
        self.assertEqual(definition.depends_on(NODE_LAYOUT), (NODE_PREPARE,))
        self.assertEqual(
            definition.depends_on(NODE_ASSEMBLE),
            (NODE_REPORT, NODE_LAYOUT),
        )
        self.assertEqual(definition.depends_on(NODE_TITLES), (NODE_TRANSLATE, NODE_POLISH))


class TestPlanner(unittest.TestCase):
    @staticmethod
    def _store(tmp: str) -> RunStore:
        store = RunStore(tmp)
        doc = Document(
            title="Book",
            fmt="text",
            source_lang="en",
            target_lang="zh",
            source_path="source.txt",
            chapters=[
                Chapter(
                    index=0, title="Chapter", segments=[Segment(index=0, source="A paragraph.")]
                ),
                Chapter(index=1, title="Notes", segments=[Segment(index=0, source="Note.")]),
            ],
        )
        state = store.stage_document(
            doc, RunIdentity(source_bytes_sha256="source", source_lang="en", target_lang="zh")
        )
        state["initialized"] = True
        store.save_manifest(state)
        return store

    def test_disabled_polish_skip_is_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            plan = Planner(build_workflow_definition()).build_plan(
                goal=GOAL_RUN_ALL,
                store=store,
                policy=WorkflowPolicy(polish=False),
                prescan=PrescanInputs(),
            )
            body = [e for stage in plan.stages for e in stage.entries if e.ci == 0]
            self.assertEqual(
                [(e.node_id, e.action, e.finalize_chapter) for e in body],
                [(NODE_TRANSLATE, "run", False), (NODE_POLISH, "skip", True)],
            )

    def test_quality_adds_polish_after_translation(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            plan = Planner(build_workflow_definition()).build_plan(
                goal=GOAL_RUN_ALL,
                store=store,
                policy=WorkflowPolicy(polish=True),
                prescan=PrescanInputs(),
            )
            body = [e.node_id for stage in plan.stages for e in stage.entries if e.ci == 0]
            self.assertEqual(body, [NODE_TRANSLATE, NODE_POLISH])

    def test_plan_uses_only_registered_nodes(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            plan = Planner(build_workflow_definition()).build_plan(
                goal=GOAL_RUN_ALL, store=store, policy=WorkflowPolicy(), prescan=PrescanInputs()
            )
            keys = plan.entry_keys()
            allowed = {
                "prepare",
                "analyze",
                "mine_terms",
                "layout",
                "name_terms",
                "translate",
                "polish",
                "titles",
                "deterministic_qa",
                "repair",
                "report",
                "assemble",
            }
            self.assertTrue(all(key.split(":", 1)[0] in allowed for key in keys))

    def test_prescan_assembly_fingerprint_uses_effective_output_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            for chapter in store.load_state().chapters:
                saved = store.load_chapter(chapter.index)
                saved.text_segments[0].target = f"T{chapter.index}"
                store.save_chapter(saved)
                progress = store.load_progress(chapter.index)
                progress.status = "done"
                store.save_progress(chapter.index, progress)
            config = Config()
            context = SimpleNamespace(
                output=config.output,
                output_digest="theme-digest",
                layout_inventory=None,
                layout_profile=None,
                output_format="epub",
                theme_bundle=None,
            )
            goal = assemble_goal(out_format="epub")

            actual = build_prescan_inputs(
                config, store, WorkflowPolicy(), context, goal
            ).assemble_fingerprint()
            expected = fingerprints.assemble_input_fingerprint(
                "T0\nT1",
                mono=True,
                bilingual=True,
                out_format="epub",
                bilingual_order="target_first",
                output_digest="theme-digest",
            )

            self.assertEqual(actual, expected)


class TestTranslationPolicy(unittest.TestCase):
    @staticmethod
    def _legacy_store(tmp: str) -> RunStore:
        store = TestPlanner._store(tmp)
        raw = store.load_manifest()
        raw["identity"].pop("translation_policy_version")
        keys = ["prepare", "analyze", "mine_terms", "name_terms", "titles"]
        keys += ["deterministic_qa", "repair", "report"]
        for chapter in raw["chapters"]:
            ci = chapter["index"]
            keys.extend([f"translate:{ci}", f"polish:{ci}"])
            raw["progress"][str(ci)]["status"] = "done"
            saved = store.load_chapter(ci)
            saved.text_segments[0].target = f"Saved translation {ci}"
            store.save_chapter(saved)
        raw["nodes"] = {
            key: {"node_id": key, "status": "succeeded", "input_fingerprint": f"old-{key}"}
            for key in keys
        }
        store.write_json(store.manifest_path, raw)
        store.save_analysis({"style_guide": "Saved style"})
        return store

    def test_old_or_unknown_policy_rejects_writing_before_reading_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._legacy_store(tmp)
            for version in (None, TRANSLATION_POLICY_VERSION + 1):
                raw = store.read_json(store.manifest_path)
                if version is not None:
                    raw["identity"]["translation_policy_version"] = version
                    store.write_json(store.manifest_path, raw)
                before = Path(store.manifest_path).read_bytes()
                for phase in ("prepare", "prescan", "translate", "titles", "repair", "polish"):
                    with self.subTest(version=version, phase=phase):
                        with self.assertRaises(IdentityMismatchError):
                            build_prescan_inputs(
                                Config(),
                                store,
                                WorkflowPolicy(),
                                None,
                                ExecutionGoal(name=phase, phases=(phase,)),
                            )
                        self.assertEqual(Path(store.manifest_path).read_bytes(), before)
                self.assertEqual(
                    store.load_chapter(0).text_segments[0].target, "Saved translation 0"
                )
                self.assertEqual(store.load_analysis(), {"style_guide": "Saved style"})

    def test_old_complete_run_can_plan_qa_and_export_without_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._legacy_store(tmp)
            before = Path(store.manifest_path).read_bytes()
            self.assertEqual(store.load_state().identity.translation_policy_version, 0)
            for goal in (qa_goal(), assemble_goal(out_format="txt")):
                with self.subTest(goal=goal.name):
                    prescan = build_prescan_inputs(Config(), store, WorkflowPolicy(), None, goal)
                    plan = Planner(build_workflow_definition()).build_plan(
                        goal=goal, store=store, policy=WorkflowPolicy(), prescan=prescan
                    )
                    self.assertEqual(
                        plan.entry_keys(),
                        {"deterministic_qa"} if goal.name == "qa" else {"layout", "assemble"},
                    )
                    self.assertEqual(Path(store.manifest_path).read_bytes(), before)
                    self.assertEqual(
                        store.load_chapter(0).text_segments[0].target, "Saved translation 0"
                    )

    def test_forced_report_reuses_completed_repair_only_for_older_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._legacy_store(tmp)
            planner = Planner(build_workflow_definition())
            goal = report_goal()

            legacy_plan = planner.build_plan(
                goal=goal,
                store=store,
                policy=WorkflowPolicy(),
                prescan=PrescanInputs(),
            )
            self.assertEqual(legacy_plan.entry_keys(), {NODE_REPORT})

            state = store.load_state()
            state.identity.translation_policy_version = TRANSLATION_POLICY_VERSION
            store.save_state(state)
            current_plan = planner.build_plan(
                goal=goal,
                store=store,
                policy=WorkflowPolicy(),
                prescan=PrescanInputs(),
            )
            self.assertEqual(current_plan.entry_keys(), {NODE_REPAIR, NODE_REPORT})

    def test_old_incomplete_export_rejects_implicit_model_work_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._legacy_store(tmp)
            raw = store.read_json(store.manifest_path)
            del raw["nodes"]["translate:0"]
            store.write_json(store.manifest_path, raw)
            before = Path(store.manifest_path).read_bytes()
            goal = assemble_goal(out_format="txt")
            prescan = build_prescan_inputs(Config(), store, WorkflowPolicy(), None, goal)
            with self.assertRaises(IdentityMismatchError):
                Planner(build_workflow_definition()).build_plan(
                    goal=goal, store=store, policy=WorkflowPolicy(), prescan=prescan
                )
            self.assertEqual(Path(store.manifest_path).read_bytes(), before)
            self.assertEqual(store.load_chapter(0).text_segments[0].target, "Saved translation 0")

    def test_staging_stamps_new_identity_without_mutating_supplied_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            identity = RunIdentity(source_lang="en", target_lang="zh")
            doc = Document(
                title="Book", fmt="text", source_lang="en", target_lang="zh", chapters=[]
            )
            store.save_manifest(store.stage_document(doc, identity))
            self.assertEqual(identity.translation_policy_version, 0)
            self.assertEqual(
                store.load_state().identity.translation_policy_version, TRANSLATION_POLICY_VERSION
            )
            prescan = build_prescan_inputs(Config(), store, WorkflowPolicy(), None, GOAL_RUN_ALL)
            plan = Planner(build_workflow_definition()).build_plan(
                goal=ExecutionGoal(name="prepare", phases=("prepare",)),
                store=store,
                policy=WorkflowPolicy(),
                prescan=prescan,
            )
            self.assertEqual(plan.entry_keys(), {"prepare", "analyze"})

    def test_policy_version_changes_analysis_translation_and_polish_fingerprints(self):
        def values():
            return (
                fingerprints.analyze_input_fingerprint("Source"),
                fingerprints.translate_input_fingerprint(
                    "Source",
                    "en",
                    "zh",
                    style_brief="",
                    punctuation_normalize=True,
                    honorific_strategy="keep_style",
                    glossary_scope="chapter",
                    single_segment_translation=True,
                ),
                fingerprints.polish_input_fingerprint(
                    "Source", "en", "", punctuation_normalize=True
                ),
            )

        current = values()
        with patch.object(
            fingerprints, "TRANSLATION_POLICY_VERSION", TRANSLATION_POLICY_VERSION + 1
        ):
            changed = values()
        for name, before, after in zip(
            ("analyze", "translate", "polish"), current, changed, strict=True
        ):
            with self.subTest(node=name):
                self.assertNotEqual(before, after)
