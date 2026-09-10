"""Facade contract tests using spy services for routing, ordering, locks and argument
forwarding.
Use the real Orchestrator with substituted private services and no domain I/O, testing
workflow composition only.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

from trans_novel.config import Config
from trans_novel.pipeline.orchestrator import Orchestrator


class _RecordingStore:
    """Fake state store recording lock scopes and run-level events."""

    def __init__(self):
        self.lock_events: list[str] = []
        self.assemble_lock_events: list[str] = []
        self.events: list[tuple[str, dict]] = []

    @contextmanager
    def lock(self):
        self.lock_events.append("lock:enter")
        try:
            yield
        finally:
            self.lock_events.append("lock:exit")

    @contextmanager
    def assemble_lock(self):
        self.assemble_lock_events.append("assemble_lock:enter")
        try:
            yield
        finally:
            self.assemble_lock_events.append("assemble_lock:exit")

    def log_event(self, event, **payload):
        self.events.append((event, payload))

    def load_usage(self):
        return None

    def recover_usage(self):
        """No pending ledger transactions exist in the facade-only fixture."""

    def save_usage(self, data):
        self.events.append(("usage_saved", {"usage": data}))


class TestOrchestratorContract(unittest.TestCase):
    """Orchestration contracts with spy services."""

    def _orchestrator(self, review: bool = False, review_autofix: bool = False) -> Orchestrator:
        cfg = Config.from_dict(
            {
                "llm": {"preset": "fake"},
                "pipeline": {"review": review, "review_autofix": review_autofix},
            }
        )
        orch = Orchestrator(cfg)
        self.preparation = MagicMock(spec=type(orch._preparation))
        orch._preparation = self.preparation
        self.translation = MagicMock(spec=type(orch._translation))
        orch._translation = self.translation
        self.review = MagicMock(spec=type(orch._review))
        orch._review = self.review
        self.review_autofix = MagicMock(spec=type(orch._review_autofix))
        orch._review_autofix = self.review_autofix
        self.review_autofix.resume_pending.return_value = None
        self.report = MagicMock(spec=type(orch._report))
        orch._report = self.report
        self.assembly = MagicMock(spec=type(orch._assembly))
        orch._assembly = self.assembly
        # Finalization requires a usable glossary scope supplied by the spy.
        self.report.glossary_scope.side_effect = lambda store, needed: self._glossary_scope()
        return orch

    @staticmethod
    @contextmanager
    def _glossary_scope():
        yield Mock()

    def _manifest(self):
        return {"chapters": [{"index": 0}, {"index": 1}]}

    def test_run_routes_prepare_then_translation_under_lock(self):
        """Prepare, restore language, build synopsis and translate under the book lock with
        unchanged arguments.
        """
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.preparation.activate.return_value = self._manifest()
        self.preparation.ensure_understanding.return_value = "全书概览"
        self.translation.run.return_value = store
        progress = Mock()

        result = orch.run("novel.txt", only_chapter=1, progress=progress)

        self.assertIs(result, store)
        self.preparation.prepare.assert_called_once_with("novel.txt", progress=progress)
        self.preparation.activate.assert_called_once_with(store)
        self.preparation.ensure_understanding.assert_called_once_with(store, progress=progress)
        self.translation.run.assert_called_once_with(
            store,
            book_synopsis="全书概览",
            only_chapter=1,
            progress=progress,
        )
        self.assertEqual(store.lock_events, ["lock:enter", "lock:exit"])

    def test_run_rejects_unknown_chapter_before_translation(self):
        """Reject unknown chapter indices before translation and propagate the validation
        error.
        """
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.preparation.activate.return_value = {"chapters": [{"index": 0}]}

        with self.assertRaisesRegex(ValueError, "Chapter index 7 does not exist"):
            orch.run("novel.txt", only_chapter=7)

        self.preparation.ensure_understanding.assert_not_called()
        self.translation.run.assert_not_called()

    def test_run_propagates_translation_exception_and_short_circuits(self):
        """Stop subsequent stages on translation failure while preserving the original
        exception.
        """
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.preparation.activate.return_value = self._manifest()
        self.preparation.ensure_understanding.return_value = ""
        boom = RuntimeError("翻译失败")

        self.translation.run.side_effect = boom
        with self.assertRaises(RuntimeError) as ctx:
            orch.run("novel.txt")
        self.assertIs(ctx.exception, boom)
        self.translation.run.assert_called_once()

    def test_run_review_uses_existing_state_fast_path_under_lock(self):
        """Review-only uses fast state lookup and a locked session without reporting or export."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.locate_existing.return_value = store
        self.review.session_terms.return_value = ["术语"]
        self.review.run_session.return_value = Mock(
            issues=[{"x": 1}], changes=[], result={"r": 1}, run_dir="reviews/1"
        )
        progress = Mock()

        result = orch.run_review("novel.txt", progress=progress)

        self.preparation.locate_existing.assert_called_once_with("novel.txt", progress=progress)
        self.review.session_terms.assert_called_once_with(store)
        self.review.run_session.assert_called_once_with(store, ["术语"], progress=progress)
        self.review_autofix.resume_pending.assert_called_once_with(store, progress=progress)
        self.review_autofix.run.assert_not_called()
        self.assertEqual(
            result,
            {
                "store": store,
                "review_issues": [{"x": 1}],
                "review_changes": [],
                "review_result": {"r": 1},
                "review_dir": "reviews/1",
            },
        )
        self.assertEqual(store.lock_events, ["lock:enter", "lock:exit"])
        self.report.build_and_save.assert_not_called()
        self.assembly.assemble_live.assert_not_called()

    def test_run_steps_review_only_routes_to_review_fast_path(self):
        """Review-only run_steps uses the same fast path as standalone run_review."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.locate_existing.return_value = store
        self.review.session_terms.return_value = []
        self.review.run_session.return_value = Mock(
            issues=[], changes=[], result={"r": 1}, run_dir="reviews/2"
        )

        result = orch.run_steps("novel.txt", {"review"})

        self.preparation.prepare.assert_not_called()
        self.translation.run.assert_not_called()
        self.review.run_session.assert_called_once()
        self.assertEqual(result["review_result"], {"r": 1})
        self.assertIsNone(result["report"])

    def test_review_autofix_runs_after_read_only_review_when_enabled(self):
        """Publish autofix only after review produces a result, returning the new outcome."""
        orch = self._orchestrator(review_autofix=True)
        store = _RecordingStore()
        self.preparation.locate_existing.return_value = store
        self.review.session_terms.return_value = ["术语"]
        review_outcome = Mock(
            issues=[{"old": 1}],
            changes=[{"chapter": 0}],
            result={"phase": "review"},
            run_dir="reviews/3",
        )
        fixed_outcome = Mock(
            issues=[{"old": 1}],
            changes=[{"chapter": 0}],
            result={"phase": "autofix"},
            run_dir="reviews/3",
        )
        self.review.run_session.return_value = review_outcome
        self.review_autofix.run.return_value = fixed_outcome

        result = orch.run_review("novel.txt")

        self.review_autofix.run.assert_called_once_with(
            store,
            review_outcome,
            ["术语"],
            progress=None,
        )
        self.assertEqual(result["review_result"], {"phase": "autofix"})

    def test_run_assemble_uses_snapshot_fast_path_without_run_lock(self):
        """Assembly-only uses the snapshot path without the book lock and forwards every format
        option.
        """
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.locate_existing.return_value = store
        self.assembly.assemble_snapshot.return_value = ["out.epub"]
        progress = Mock()

        result = orch.run_assemble(
            "novel.txt",
            out_format="epub",
            out_path="out.epub",
            pdf_engine="weasyprint",
            progress=progress,
        )

        self.preparation.locate_existing.assert_called_once_with("novel.txt", progress=progress)
        self.assembly.assemble_snapshot.assert_called_once_with(
            store,
            input_path="novel.txt",
            progress=progress,
            out_format="epub",
            out_path="out.epub",
            pdf_engine="weasyprint",
        )
        self.assertEqual(store.lock_events, [])
        self.assertEqual(store.assemble_lock_events, [])
        self.assertEqual(result["output"], "out.epub")
        self.assertEqual(result["outputs"], ["out.epub"])
        self.assertIsNone(result["report"])
        self.assertIsNone(result["review_dir"])

    def test_run_steps_assemble_only_uses_snapshot_fast_path(self):
        """Assembly-only run_steps neither waits for translation nor calls prepare."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.locate_existing.return_value = store
        self.assembly.assemble_snapshot.return_value = ["out.epub"]

        result = orch.run_steps("novel.txt", {"assemble"}, out_format="txt", pdf_engine="fpdf2")

        self.preparation.prepare.assert_not_called()
        self.assembly.assemble_snapshot.assert_called_once_with(
            store,
            input_path="novel.txt",
            progress=None,
            out_format="txt",
            out_path=None,
            pdf_engine="fpdf2",
        )
        self.assertEqual(result["output"], "out.epub")

    def test_run_steps_report_only_uses_prepare_not_locate_existing(self):
        """Other combinations without translation retain current preparation behavior."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.report.build_and_save.return_value = {"report": True}

        result = orch.run_steps("novel.txt", {"report"})

        self.preparation.prepare.assert_called_once_with("novel.txt", progress=None)
        self.preparation.locate_existing.assert_not_called()
        self.report.build_and_save.assert_called_once()
        self.assertEqual(result["report"], {"report": True})
        self.assertEqual(store.lock_events, ["lock:enter", "lock:exit"])

    def test_full_pipeline_steps_order_and_result_assembly(self):
        """Translate first, then reacquire the lock for reporting and live export."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.preparation.activate.return_value = self._manifest()
        self.preparation.ensure_understanding.return_value = ""
        self.translation.run.return_value = store
        self.report.build_and_save.return_value = {"report": True}
        self.assembly.assemble_live.return_value = ["out.epub"]

        calls: list[str] = []
        self.report.build_and_save.side_effect = lambda *a, **k: (
            calls.append("report") or {"report": True}
        )
        self.assembly.assemble_live.side_effect = lambda *a, **k: (
            calls.append("assemble") or ["out.epub"]
        )
        self.translation.run.side_effect = lambda *a, **k: calls.append("translate") or store

        result = orch.run_steps("novel.txt", {"translate", "report", "assemble"})

        # Reenter the lock for finalization after translation; skip review unless requested.
        self.assertEqual(calls, ["translate", "report", "assemble"])
        self.review.run_session.assert_not_called()
        self.assertEqual(
            store.lock_events,
            ["lock:enter", "lock:exit", "lock:enter", "lock:exit"],
        )
        self.assertEqual(
            result,
            {
                "store": store,
                "output": "out.epub",
                "outputs": ["out.epub"],
                "report": {"report": True},
                "review_issues": [],
                "review_changes": [],
                "review_result": None,
                "review_dir": None,
            },
        )

    def test_full_pipeline_with_review_includes_review_between_translate_and_report(self):
        """Combinations containing translation finish it before locked review, reporting and
        live assembly.
        """
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.preparation.activate.return_value = self._manifest()
        self.preparation.ensure_understanding.return_value = ""
        self.translation.run.return_value = store
        self.review.run_session.return_value = Mock(
            issues=[], changes=[], result={"r": 1}, run_dir="reviews/3"
        )
        self.report.build_and_save.return_value = {"report": True}
        self.assembly.assemble_live.return_value = ["out.epub"]

        calls: list[str] = []
        self.review.run_session.side_effect = lambda *a, **k: (
            calls.append("review")
            or Mock(issues=[], changes=[], result={"r": 1}, run_dir="reviews/3")
        )
        self.report.build_and_save.side_effect = lambda *a, **k: (
            calls.append("report") or {"report": True}
        )
        self.assembly.assemble_live.side_effect = lambda *a, **k: (
            calls.append("assemble") or ["out.epub"]
        )
        self.translation.run.side_effect = lambda *a, **k: calls.append("translate") or store

        result = orch.run_steps("novel.txt", {"translate", "review", "report", "assemble"})

        self.assertEqual(calls, ["translate", "review", "report", "assemble"])
        self.assertEqual(result["review_result"], {"r": 1})
        self.assertEqual(result["report"], {"report": True})

    def test_review_failure_short_circuits_report_and_assemble(self):
        """Review failure skips reporting/export and propagates unchanged."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.preparation.activate.return_value = self._manifest()
        self.preparation.ensure_understanding.return_value = ""
        self.translation.run.return_value = store
        boom = RuntimeError("review 失败")
        self.review.run_session.side_effect = boom

        with self.assertRaises(RuntimeError) as ctx:
            orch.run_steps("novel.txt", {"translate", "review", "report", "assemble"})
        self.assertIs(ctx.exception, boom)
        self.report.build_and_save.assert_not_called()
        self.assembly.assemble_live.assert_not_called()

    def test_report_failure_short_circuits_assemble(self):
        """Report failure skips export and propagates unchanged."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        self.preparation.activate.return_value = self._manifest()
        self.preparation.ensure_understanding.return_value = ""
        self.translation.run.return_value = store
        boom = RuntimeError("report 失败")
        self.report.build_and_save.side_effect = boom

        with self.assertRaises(RuntimeError) as ctx:
            orch.run_steps("novel.txt", {"translate", "report", "assemble"})
        self.assertIs(ctx.exception, boom)
        self.assembly.assemble_live.assert_not_called()

    def test_run_all_with_review_enabled_requests_review(self):
        """run_all includes review when enabled in configuration."""
        orch = self._orchestrator(review=True)
        with patch.object(orch, "run_steps", return_value={"sentinel": True}) as spy:
            result = orch.run_all("novel.txt")
        spy.assert_called_once()
        steps = spy.call_args.args[1]
        self.assertEqual(steps, {"translate", "review", "report", "assemble"})
        self.assertEqual(result, {"sentinel": True})

    def test_run_all_without_review_excludes_review(self):
        """run_all omits review when disabled in configuration."""
        orch = self._orchestrator(review=False)
        with patch.object(orch, "run_steps", return_value={}) as spy:
            orch.run_all("novel.txt")
        self.assertEqual(spy.call_args.args[1], {"translate", "report", "assemble"})

    def test_prepare_delegates_thinly(self):
        """prepare is a thin delegation preserving arguments and return values."""
        orch = self._orchestrator()
        store = _RecordingStore()
        self.preparation.prepare.return_value = store
        progress = Mock()

        result = orch.prepare("novel.txt", progress=progress)

        self.assertIs(result, store)
        self.preparation.prepare.assert_called_once_with("novel.txt", progress=progress)

    def test_facade_still_exposes_config_and_client(self):
        """Preserve public access to Orchestrator.config and Orchestrator.client."""
        cfg = Config.from_dict({"llm": {"preset": "fake"}})
        orch = Orchestrator(cfg)
        self.assertIs(orch.config, cfg)
        self.assertIsNotNone(orch.client)


if __name__ == "__main__":
    unittest.main()
