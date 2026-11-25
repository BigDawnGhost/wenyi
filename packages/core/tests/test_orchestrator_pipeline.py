"""Offline end-to-end orchestration and resume tests using FakeClient."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import fake_pipeline_config as _config
from tests.sample_data import write_sample_txt


class TestRunSteps(unittest.TestCase):
    def test_subset_only_assemble(self):
        """Assembly-only run_steps is idempotent and makes no translation calls."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run_steps(txt, {"translate"})
            # Assembly only; no new translation calls.
            client2 = FakeClient(handler=routing_handler)
            res = Orchestrator(cfg, client=client2).run_steps(txt, {"assemble"})
            self.assertTrue(res["output"].endswith(".epub"))
            self.assertTrue(os.path.isfile(res["output"]))
            translate_calls = [
                c for c in client2.calls if "literary translator" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(translate_calls), 0)


class TestOperationRouting(unittest.TestCase):
    def test_tasks_use_registered_operations(self):
        """Route tasks by operation without implicit output caps."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)
            orch.run_review(txt)

            expect = {
                "chapter digest writer": {"synopsis.chapter"},
                "whole-book synopsis writer": {"synopsis.book"},
                "terminology and forms-of-address extractor": {"glossary.extract"},
                "translation reviewer": {"review.scan"},
                "literary translator": {"translation.body", "polish.body"},
            }
            seen = set()
            for c in client.calls:
                system = c["messages"][0]["content"]
                for marker, operations in expect.items():
                    if marker in system:
                        self.assertIn(c["operation"], operations, marker)
                        seen.add(marker)
                        self.assertIsNone(c["max_tokens"])
            self.assertEqual(seen, set(expect), "各类调用都应出现")


class TestProgressLabels(unittest.TestCase):
    def test_progress_covers_preparation_and_output_stages(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            events: list[tuple[int, int, str]] = []
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))

            def progress(done: int, total: int, label: str, /) -> None:
                events.append((done, total, label))

            result = orch.run_steps(txt, {"translate", "assemble"}, progress=progress)
            self.assertTrue(
                all(row["title_translated"] for row in result["store"].load_manifest()["chapters"])
            )

            labels = [label for _, _, label in events]
            expected = [
                "progress.stage.parsing",
                "progress.stage.prescan",
                "progress.stage.style",
                "progress.stage.synopsis",
                "Translating chapter titles…",
                "Translating chapter titles",
                "progress.stage.translationDone",
                "progress.stage.assembling",
            ]
            positions = [labels.index(label) for label in expected]
            self.assertEqual(positions, sorted(positions), labels)
            self.assertTrue(
                any(
                    done == total > 0 and label == "progress.stage.prescan"
                    for done, total, label in events
                )
            )
            self.assertIn((0, 0, "progress.stage.synopsis"), events)
            self.assertNotIn("Generating report…", labels)


class TestRunAll(unittest.TestCase):
    def test_continuous_pipeline_outputs_epub(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = Config.from_dict(
                {
                    "language": {"source": "auto", "target": "zh"},
                    "llm": {"preset": "fake"},
                    "pipeline": {
                        "review": True,
                        "review_autofix": False,
                        "polish": True,
                    },
                    "paths": {"state_dir": state},
                }
            )
            seen = []
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            result = orch.run_all(
                txt,
                progress=lambda done, total, label, payload=None: seen.append(
                    (done, total, label, payload)
                ),
                out_format="epub",
            )
            self.assertTrue(result["output"].endswith(".epub"))
            self.assertTrue(zipfile.is_zipfile(result["output"]))
            # Progress callbacks run and finish with done equal to total.
            self.assertTrue(seen)
            self.assertEqual(seen[-1][0], seen[-1][1])
            self.assertIn((0, 2, "Translating chapter titles…", None), seen)
            self.assertIn((2, 2, "Translating chapter titles", {"batch": 0, "batch_size": 2}), seen)
            self.assertIn((2, 2, "Repairing fallback annotations", {"chapter": 1}), seen)
            # Model detection resolves auto source to ja.
            self.assertEqual(cfg.source_lang, "ja")
            with open(result["store"].event_log_path, "r", encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            event_names = [e["event"] for e in events]
            self.assertIn("run_initialized", event_names)
            self.assertIn("batch_translated", event_names)
            self.assertNotIn("report_saved", event_names)
            self.assertNotIn("report", result)
            self.assertFalse(os.path.exists(os.path.join(result["store"].run_dir, "report.json")))
            self.assertIn("assembled", event_names)
            translated = next(e for e in events if e["event"] == "batch_translated")
            self.assertTrue(translated["segments"])
            self.assertIn("source", translated["segments"][0])
            self.assertIn("target", translated["segments"][0])


if __name__ == "__main__":
    unittest.main()
