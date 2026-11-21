"""Offline language detection, export and complete-workflow tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from wenyi_core.config import Config
from wenyi_core.i18n.languages import honorific_rule
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import require_file_storage
from tests.sample_data import write_sample_txt


class TestModelLanguageDetection(unittest.TestCase):
    def _cfg(self, state: str) -> Config:
        return Config.from_dict(
            {
                "language": {"source": "auto", "target": "zh"},
                "llm": {"preset": "fake"},
                "pipeline": {"book_understanding": False},
                "paths": {"state_dir": state},
            }
        )

    def test_auto_uses_model_detection(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "language detector" in messages[0]["content"]:
                    return json.dumps({"language": "russian"}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            store = require_file_storage(
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)
            )
            self.assertEqual(cfg.source_lang, "ru")
            self.assertEqual(store.load_manifest()["source_lang"], "ru")

    def test_auto_detection_failure_requires_user_source(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "language detector" in messages[0]["content"]:
                    return json.dumps({"language": ""}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            with self.assertRaisesRegex(ValueError, "language.source"):
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)

    def test_explicit_same_source_and_target_stops_before_model_calls(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = Config.from_dict(
                {
                    "language": {"source": "ja", "target": "ja-JP"},
                    "llm": {"preset": "fake"},
                    "paths": {"state_dir": os.path.join(d, "state")},
                }
            )
            client = FakeClient(handler=routing_handler)

            with self.assertRaisesRegex(
                ValueError, "Source and target languages are identical .*ja"
            ):
                Orchestrator(cfg, client=client).prepare(txt)

            self.assertEqual(client.calls, [])

    def test_auto_detected_source_matching_target_stops_before_analysis(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "language detector" in messages[0]["content"]:
                    return json.dumps({"language": "chinese"}, ensure_ascii=False)
                raise AssertionError("相同语言不应继续进入分析或翻译")

            with self.assertRaisesRegex(
                ValueError, "Source and target languages are identical .*zh"
            ):
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)


class TestStandaloneExport(unittest.TestCase):
    def test_export_preserves_saved_punctuation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "novel.txt"
            source.write_text("Hello world.", encoding="utf-8")
            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": "zh"},
                    "llm": {"preset": "fake"},
                    "pipeline": {
                        "book_understanding": False,
                        "polish": False,
                        "annotation_alignment": False,
                    },
                    "paths": {"state_dir": str(Path(directory) / "state")},
                }
            )

            def handler(messages, tier, json_mode):
                if "literary translator" in messages[0]["content"]:
                    return json.dumps({"translations": ["他说,真的吗?"]}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            orchestrator = Orchestrator(config, client=FakeClient(handler=handler))
            store = orchestrator.run(str(source))
            self.assertEqual(store.load_chapter(0).text_segments[0].target, "他说,真的吗?")

            output = Path(
                orchestrator.run_assemble(
                    str(source),
                    out_format="txt",
                    out_path=str(Path(directory) / "novel.zh.txt"),
                )["output"]
            )
            self.assertIn("他说,真的吗?", output.read_text(encoding="utf-8"))
            self.assertEqual(store.load_chapter(0).text_segments[0].target, "他说,真的吗?")


class TestLanguageProfile(unittest.TestCase):
    def test_language_pair_requires_stable_honorific_choice(self):
        rule = honorific_rule("ja", "zh")

        self.assertIn("use it consistently for that relationship throughout the book", rule)
        self.assertNotIn("可酌情保留", rule)


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
                progress=lambda done, total, label: seen.append((done, total)),
                out_format="epub",
            )
            self.assertTrue(result["output"].endswith(".epub"))
            self.assertTrue(zipfile.is_zipfile(result["output"]))
            # Progress callbacks run and finish with done equal to total.
            self.assertTrue(seen)
            self.assertEqual(seen[-1][0], seen[-1][1])
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
