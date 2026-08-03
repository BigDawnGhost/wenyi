"""新功能测试（离线）：模型语言检测、标点规范化、术语 AI 审计统一、连续全流程。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from bs4 import BeautifulSoup

from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt
from trans_novel.agents import prompts
from trans_novel.agents.langprofile import (
    honorific_rule,
    term_guidance,
    translate_example,
    translate_guidance,
)
from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.checks import length_flags_for_direction
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.postprocess.punct import normalize_en, normalize_zh, normalize_zh_segments


class TestModelLanguageDetection(unittest.TestCase):
    def _cfg(self, state: str) -> Config:
        return Config.from_dict(
            {
                "language": {"source": "auto", "target": "zh"},
                "llm": {
                    "provider": "fake",
                    "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
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
                if "语言识别器" in messages[0]["content"]:
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
                if "语言识别器" in messages[0]["content"]:
                    return json.dumps({"language": ""}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            with self.assertRaisesRegex(RuntimeError, "language.source"):
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)

    def test_explicit_same_source_and_target_stops_before_model_calls(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = Config.from_dict(
                {
                    "language": {"source": "ja", "target": "ja-JP"},
                    "llm": {"provider": "fake"},
                    "paths": {"state_dir": os.path.join(d, "state")},
                }
            )
            client = FakeClient(handler=routing_handler)

            with self.assertRaisesRegex(ValueError, "源语言与目标语言相同（ja）"):
                Orchestrator(cfg, client=client).prepare(txt)

            self.assertEqual(client.calls, [])

    def test_unsupported_target_stops_before_model_calls(self):
        cfg = Config.from_dict(
            {
                "language": {"source": "ja", "target": "fr"},
                "llm": {"provider": "fake"},
            }
        )
        client = FakeClient(handler=routing_handler)

        with self.assertRaisesRegex(ValueError, "language.target 目前仅支持 zh 或 en"):
            Orchestrator(cfg, client=client)

        self.assertEqual(client.calls, [])

    def test_auto_detected_source_matching_target_stops_before_analysis(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "语言识别器" in messages[0]["content"]:
                    return json.dumps({"language": "chinese"}, ensure_ascii=False)
                raise AssertionError("相同语言不应继续进入分析或翻译")

            with self.assertRaisesRegex(ValueError, "源语言与目标语言相同（zh）"):
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)

    def test_english_target_rejects_non_chinese_auto_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(directory, "state"))
            cfg.target_lang = "en"

            def handler(messages, tier, json_mode):
                if "语言识别器" in messages[0]["content"]:
                    return json.dumps({"language": "japanese"}, ensure_ascii=False)
                raise AssertionError("不支持的翻译方向不应进入分析")

            with self.assertRaisesRegex(ValueError, "英文目标目前仅支持中文源文本"):
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)

    def test_resume_rejects_changed_target_language(self):
        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(directory, "state")
            english = Config.from_dict(
                {
                    "language": {"source": "zh", "target": "en"},
                    "llm": {"provider": "fake"},
                    "pipeline": {"book_understanding": False},
                    "paths": {"state_dir": state},
                }
            )
            Orchestrator(english, client=FakeClient(handler=routing_handler)).prepare(txt)

            changed = Config.from_dict(
                {
                    "language": {"source": "auto", "target": "zh"},
                    "llm": {"provider": "fake"},
                    "paths": {"state_dir": state},
                }
            )
            with self.assertRaisesRegex(ValueError, "不能在同一状态目录切换翻译方向"):
                Orchestrator(changed, client=FakeClient(handler=routing_handler)).prepare(txt)


class TestPunct(unittest.TestCase):
    def test_japanese_quotes(self):
        self.assertEqual(normalize_zh("「你好」"), "“你好”")
        self.assertEqual(normalize_zh("『书名』"), "‘书名’")

    def test_halfwidth_to_full_in_cjk(self):
        self.assertEqual(normalize_zh("他说,真的吗?"), "他说，真的吗？")

    def test_no_harm_to_english_numbers(self):
        self.assertEqual(normalize_zh("9.11 vs 9.8"), "9.11 vs 9.8")
        self.assertEqual(normalize_zh("Mr.王"), "Mr.王")

    def test_ellipsis_and_dash(self):
        self.assertEqual(normalize_zh("等等...走了--他笑了"), "等等……走了——他笑了")

    def test_word_final_apostrophe_is_a_right_apostrophe(self):
        self.assertEqual(normalize_zh("James' book"), "James’ book")

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
        with self.assertRaisesRegex(ValueError, "数量必须一致"):
            normalize_zh_segments(["第一段"], [])

    def test_english_target_uses_english_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config.from_dict(
                {
                    "language": {"source": "zh", "target": "en"},
                    "llm": {"provider": "fake"},
                    "paths": {"state_dir": os.path.join(directory, "state")},
                }
            )
            orchestrator = Orchestrator(cfg, client=FakeClient())

        self.assertTrue(orchestrator._punctuation_enabled())
        self.assertEqual(
            orchestrator._normalize_target("“Wait——really？”"),
            "“Wait—really?”",
        )

    def test_english_normalization_is_conservative(self):
        self.assertEqual(
            normalize_en("「Wait」，she said……（quietly）。"),
            "“Wait”, she said… (quietly).",
        )
        self.assertEqual(
            normalize_en("It cost 1，000 at 12：30。Next"),
            "It cost 1,000 at 12:30. Next",
        )
        self.assertEqual(
            normalize_en("Version 1。2 is at https：//example.com。Next"),
            "Version 1.2 is at https://example.com. Next",
        )
        self.assertEqual(normalize_en("James' book costs 9.11."), "James' book costs 9.11.")


class TestLanguageProfile(unittest.TestCase):
    def test_keep_style_requires_stable_honorific_choice(self):
        rule = honorific_rule("keep_style")

        self.assertIn("确定后同一关系全书沿用", rule)
        self.assertNotIn("可酌情保留", rule)

    def test_chinese_to_english_guidance_uses_publication_conventions(self):
        guidance = translate_guidance("zh-CN", tgt="en")
        terms = term_guidance("zh", "en")

        self.assertIn("国际英文", guidance)
        self.assertIn("不得擅自添加", guidance)
        self.assertIn("无声调汉语拼音", guidance)
        self.assertIn("权威或通行英译", terms)

    def test_translate_example_is_zh_en_only(self):
        zh_en = translate_example("zh", tgt="en")

        self.assertIn("翻译示例", zh_en)
        self.assertNotIn("$", zh_en)
        self.assertEqual(translate_example("ja", tgt="zh"), "")
        self.assertEqual(translate_example("zh", tgt="zh"), "")

        rendered = prompts.render("translator_system", src="zh", tgt="en")
        rendered_ja = prompts.render("translator_system", src="ja", tgt="zh")
        self.assertIn("翻译示例", rendered)
        self.assertNotIn("翻译示例", rendered_ja)
        self.assertNotIn("$fewshot_example", rendered)
        self.assertNotIn("$fewshot_example", rendered_ja)

    def test_target_aware_prompts_do_not_request_chinese_output(self):
        translator = prompts.render("translator_system", src="zh", tgt="en")
        polisher = prompts.render("polisher_system", src="zh", tgt="en")
        reviewer = prompts.render("reviewer_system", src="zh", tgt="en")
        titles = prompts.render("title_translator_system", src="zh", tgt="en")
        analyzer = prompts.render("analyzer_system", src="zh", tgt="en")
        glossary = prompts.render("glossary_extractor_system", src="zh", tgt="en")
        backtranslate = prompts.render("backtranslate_system", src="zh", tgt="en")
        consistency = prompts.render("consistency_system", src="zh", tgt="en")

        self.assertIn("将中文小说翻译为英文", translator)
        self.assertIn("英文文学润色编辑", polisher)
        self.assertIn("suggestion 须使用英文", reviewer)
        self.assertIn("翻译为英文", titles)
        self.assertIn("建议英文译名", analyzer)
        self.assertIn("US 或 UK", analyzer)
        self.assertIn("simple past", analyzer)
        self.assertIn("he/she/they", reviewer)
        self.assertIn("英文译文", glossary)
        self.assertIn("英文译文回译成中文", backtranslate)
        self.assertIn("标点是否符合英文规范", consistency)
        self.assertNotIn("翻译为简体中文", translator)

    def test_chinese_target_remains_explicitly_simplified(self):
        translator = prompts.render("translator_system", src="ja", tgt="zh")
        punctuation = prompts.render("polisher_system", src="ja", tgt="zh")
        analyzer = prompts.render("analyzer_system", src="ja", tgt="zh")
        reviewer = prompts.render("reviewer_system", src="ja", tgt="zh")

        self.assertIn("翻译为简体中文", translator)
        self.assertIn("简体中文大陆通用全角形式", punctuation)
        self.assertIn("简体中文写作风格指南", analyzer)
        self.assertIn("他/她/它", reviewer)
        for english_only in ("US 或 UK", "simple past", "Shifu", "he/she/they"):
            self.assertNotIn(english_only, analyzer + reviewer)

    def test_fake_analyzer_routes_by_target_language(self):
        def analyze(src: str, tgt: str) -> dict:
            system = prompts.render("analyzer_system", src=src, tgt=tgt)
            return json.loads(
                routing_handler(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": "sample"},
                    ],
                    "strong",
                    True,
                )
            )

        self.assertEqual(analyze("ja", "zh")["characters"][0]["source"], "綾小路")
        self.assertEqual(analyze("en", "zh")["characters"][0]["source"], "綾小路")
        self.assertEqual(analyze("zh", "en")["characters"][0]["source"], "林远")

    def test_chinese_to_english_length_limits_allow_normal_expansion(self):
        self.assertEqual(
            length_flags_for_direction(
                ["这是一个正常长度的中文句子。"],
                ["This is an ordinarily expanded English sentence for the Chinese source."],
                "zh",
                "en",
            ),
            [],
        )
        self.assertEqual(
            length_flags_for_direction(["嗯"], ["Mm-hmm."], "zh", "en"),
            [],
        )


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
                        "provider": "fake",
                        "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
                    },
                    "pipeline": {
                        "review": True,
                        "polish": True,
                        "backtranslate_sample": 0.0,
                        "consistency_qa": True,
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
            # 进度回调被触发，且最终 done==total
            self.assertTrue(seen)
            self.assertEqual(seen[-1][0], seen[-1][1])
            # auto 通过模型检测把源语言定为 ja
            self.assertEqual(cfg.source_lang, "ja")
            # 报告含一致性字段。
            self.assertIn("consistency_issues", result["report"])
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

    def test_chinese_to_english_pipeline_outputs_english_epub(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "chinese-novel.txt")
            with open(source, "w", encoding="utf-8") as file:
                file.write("第一章\n\n" + "林远站在窗边，听见雨点敲打玻璃。他没有回头。\n\n" * 8)
            cfg = Config.from_dict(
                {
                    "language": {"source": "zh", "target": "en"},
                    "llm": {
                        "provider": "fake",
                        "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
                    },
                    "pipeline": {
                        "review": True,
                        "polish": True,
                        "consistency_qa": True,
                    },
                    "paths": {"state_dir": os.path.join(directory, "state")},
                }
            )

            result = Orchestrator(
                cfg,
                client=FakeClient(handler=routing_handler),
            ).run_all(source, out_format="epub")

            self.assertEqual(os.path.basename(result["output"]), "chinese-novel.en.epub")
            self.assertEqual(result["store"].load_manifest()["target_lang"], "en")
            with zipfile.ZipFile(result["output"]) as archive:
                names = archive.namelist()
                about_name = next(
                    name for name in names if name.endswith("trans-novel-about.xhtml")
                )
                opf_name = next(name for name in names if name.endswith(".opf"))
                chapter_name = next(name for name in names if name.endswith("ch0.xhtml"))
                about = archive.read(about_name).decode("utf-8")
                chapter = archive.read(chapter_name).decode("utf-8")
                package = BeautifulSoup(archive.read(opf_name), "xml")

            self.assertIn("About This Translation", about)
            self.assertIn("Polished", chapter)
            self.assertEqual(package.find("dc:language").get_text(strip=True), "en")
            self.assertIn("-wenyi-en", package.find("dc:title").get_text(strip=True))


if __name__ == "__main__":
    unittest.main()
