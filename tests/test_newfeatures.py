"""Offline language detection, punctuation, glossary audit and complete-workflow tests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt
from trans_novel.assemble.export_view import ExportViewStore
from trans_novel.config import Config
from trans_novel.i18n.languages import honorific_rule
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore
from trans_novel.postprocess.punct import normalize_zh_segments


class TestModelLanguageDetection(unittest.TestCase):
    def _cfg(self, state: str) -> Config:
        return Config.from_dict(
            {
                "language": {"source": "auto", "target": "zh"},
                "llm": {
                    "preset": "fake",
                    "models": {
                        "default_strong": {"provider": "default", "model": "p"},
                        "default_cheap": {"provider": "default", "model": "f"},
                    },
                },
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

            store = Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)
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


class TestPunct(unittest.TestCase):
    def test_japanese_quotes(self):
        self.assertEqual(normalize_zh_segments(["「你好」"])[0], "“你好”")
        self.assertEqual(normalize_zh_segments(["『书名』"])[0], "‘书名’")

    def test_halfwidth_to_full_in_cjk(self):
        self.assertEqual(normalize_zh_segments(["他说,真的吗?"])[0], "他说，真的吗？")

    def test_no_harm_to_english_numbers(self):
        self.assertEqual(normalize_zh_segments(["9.11 vs 9.8"])[0], "9.11 vs 9.8")
        self.assertEqual(normalize_zh_segments(["Mr.王"])[0], "Mr.王")

    def test_ellipsis_and_dash(self):
        self.assertEqual(normalize_zh_segments(["等等...走了--他笑了"])[0], "等等……走了——他笑了")

    def test_word_final_apostrophe_is_a_right_apostrophe(self):
        self.assertEqual(normalize_zh_segments(["James' book"])[0], "James’ book")

    def test_quotes_are_paired_across_split_continuations(self):
        self.assertEqual(
            normalize_zh_segments(
                ['"第一段', '第二段"', '"下一句"'],
                [False, True, False],
            ),
            ["“第一段", "第二段”", "“下一句”"],
        )

    def test_unmatched_quote_does_not_leak_into_next_paragraph(self):
        self.assertEqual(
            normalize_zh_segments(
                ['"缺少右引号', '"新的完整对话"'],
                [False, False],
            ),
            ["“缺少右引号", "“新的完整对话”"],
        )

    def test_continuation_flags_must_align_with_texts(self):
        with self.assertRaisesRegex(ValueError, "must have the same length"):
            normalize_zh_segments(["第一段"], [])

    def test_non_chinese_target_does_not_enable_chinese_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config.from_dict(
                {
                    "language": {"source": "zh", "target": "en"},
                    "llm": {"preset": "fake"},
                    "paths": {"state_dir": os.path.join(directory, "state")},
                }
            )
            orchestrator = Orchestrator(cfg, client=FakeClient())

        self.assertFalse(orchestrator._runtime.export_punctuation_enabled())

    def test_punctuation_normalization_only_changes_export_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "novel.txt"
            source.write_text("Hello world.", encoding="utf-8")
            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": "zh"},
                    "llm": {
                        "preset": "fake",
                        "models": {
                            "default_strong": {"provider": "default", "model": "p"},
                            "default_cheap": {"provider": "default", "model": "f"},
                        },
                    },
                    "pipeline": {
                        "book_understanding": False,
                        "polish": False,
                        "annotation_alignment": False,
                    },
                    "output": {"punctuation_normalize": True},
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
            self.assertIn("他说，真的吗？", output.read_text(encoding="utf-8"))
            self.assertEqual(store.load_chapter(0).text_segments[0].target, "他说,真的吗?")

            config.output.punctuation_normalize = False
            raw_output = Path(
                orchestrator.run_assemble(
                    str(source),
                    out_format="txt",
                    out_path=str(Path(directory) / "novel.raw.zh.txt"),
                )["output"]
            )
            self.assertIn("他说,真的吗?", raw_output.read_text(encoding="utf-8"))

    def test_export_copy_remaps_annotation_and_docx_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            before = "甲...乙"
            after = "甲……乙"
            placement = {
                "id": "marker",
                "target_start": 4,
                "target_end": 4,
                "status": "aligned",
            }
            store = RunStore(str(Path(directory) / "state" / "book"))
            store.save_chapter(
                Chapter(
                    index=0,
                    segments=[
                        Segment(
                            index=0,
                            source="source",
                            target=before,
                            meta={
                                "epub_annotations": {
                                    "target_digest": hashlib.sha256(before.encode()).hexdigest(),
                                    "placements": [dict(placement)],
                                },
                                "docx_styles": {
                                    "target_digest": hashlib.sha256(before.encode()).hexdigest(),
                                    "placements": [dict(placement)],
                                },
                            },
                        )
                    ],
                )
            )

            store.save_manifest({"target_lang": "zh"})
            exported = ExportViewStore(store, punctuation_normalize=True).load_chapter(0)

            self.assertEqual(exported.segments[0].target, after)
            expected_digest = hashlib.sha256(after.encode()).hexdigest()
            for key in ("epub_annotations", "docx_styles"):
                metadata = exported.segments[0].meta[key]
                self.assertEqual(metadata["target_digest"], expected_digest)
                self.assertEqual(metadata["placements"][0]["target_start"], 3)
                self.assertEqual(metadata["placements"][0]["target_end"], 3)

            persisted = store.load_chapter(0).segments[0]
            self.assertEqual(persisted.target, before)
            self.assertEqual(persisted.meta["epub_annotations"]["placements"][0]["target_start"], 4)


class TestLanguageProfile(unittest.TestCase):
    def test_keep_style_requires_stable_honorific_choice(self):
        rule = honorific_rule("keep_style")

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
                    "llm": {
                        "preset": "fake",
                        "models": {
                            "default_strong": {"provider": "default", "model": "p"},
                            "default_cheap": {"provider": "default", "model": "f"},
                        },
                    },
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
            self.assertIn("report_saved", event_names)
            self.assertIn("assembled", event_names)
            translated = next(e for e in events if e["event"] == "batch_translated")
            self.assertTrue(translated["segments"])
            self.assertIn("source", translated["segments"][0])
            self.assertIn("target", translated["segments"][0])


if __name__ == "__main__":
    unittest.main()
