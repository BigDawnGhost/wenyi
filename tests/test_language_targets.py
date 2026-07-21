"""Language-pair prompt contracts and English-target pipeline coverage."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from trans_novel import languages as language
from trans_novel.agents import prompts
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.languages.en.prompts import TEMPLATES as ENGLISH_TEMPLATES
from trans_novel.pipeline.orchestrator import Orchestrator
from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt


class TestLanguageProfiles(unittest.TestCase):
    def test_canonicalizes_common_bcp47_forms(self):
        self.assertEqual(language.canonical_tag("EN_us"), "en-US")
        self.assertEqual(language.canonical_tag("zh_hans_cn"), "zh-Hans-CN")
        self.assertEqual(
            language.canonical_tag("en-US-u-ca-gregory"),
            "en-US-u-ca-gregory",
        )
        self.assertEqual(language.base_language("EN-us"), "en")

    def test_only_simplified_chinese_aliases_share_the_legacy_identity(self):
        self.assertEqual(language.target_identity("zh-CN"), "zh")
        self.assertEqual(language.target_identity("zh-Hans"), "zh")
        self.assertEqual(language.target_identity("zh-Hant"), "zh-Hant")
        self.assertNotIn(
            "Chinese full-width",
            language.target_profile("zh-Hant").punctuation_guidance,
        )

    def test_japanese_to_english_has_pair_specific_rules(self):
        rules = language.pair_guidance("ja", "en", "keep_style")

        self.assertIn("-san", rules)
        self.assertIn("romanization", rules)
        self.assertNotIn("他/她/它", rules)

    def test_english_profile_uses_english_typography(self):
        profile = language.target_profile("en")

        self.assertIn("standard English typography", profile.punctuation_guidance)
        self.assertIn("ASCII half-width straight", profile.punctuation_guidance)
        self.assertIn("Never use curly smart", profile.punctuation_guidance)
        self.assertIn("em dash", profile.punctuation_guidance)
        self.assertIn("English-language publishing", profile.title_guidance)
        self.assertEqual(profile.chapter_digest_limit, "160 English words")


class TestEnglishPromptContracts(unittest.TestCase):
    SYSTEM_PROMPTS = (
        "translator_system",
        "reviewer_system",
        "polisher_system",
        "title_translator_system",
        "analyzer_system",
        "glossary_extractor_system",
        "backtranslate_system",
        "backtranslation_compare_system",
        "consistency_system",
        "chapter_digest_system",
        "book_synopsis_system",
    )
    FORCED_CHINESE_TARGET_PHRASES = (
        "翻译为简体中文",
        "中文译文",
        "中文润色编辑",
        "建议中文译名",
        "建议中文译法",
        "用简体中文",
        "中文梗概",
        "简体中文规范",
    )

    def test_all_pipeline_prompts_resolve_target_to_english(self):
        for name in self.SYSTEM_PROMPTS:
            with self.subTest(prompt=name):
                rendered = prompts.render(name, src="ja", tgt="en")
                self.assertIn("English", rendered)
                self.assertNotIn("$", rendered)
                for phrase in self.FORCED_CHINESE_TARGET_PHRASES:
                    self.assertNotIn(phrase, rendered)

    def test_english_bundle_contains_no_chinese_instruction_labels(self):
        banned = (
            "【",
            "你是",
            "仅输出",
            "请输出",
            "中文译文",
            "英文译文",
            "原文：",
            "译文：",
            "（无）",
            "建议：",
        )
        for name, template in ENGLISH_TEMPLATES.items():
            with self.subTest(prompt=name):
                for marker in banned:
                    self.assertNotIn(marker, template.template)

    def test_backtranslation_direction_is_english_to_japanese(self):
        rendered = prompts.render("backtranslate_system", src="ja", tgt="en")

        self.assertIn("English translation back into Japanese", rendered)

    def test_translation_prompt_keeps_json_contract(self):
        rendered = prompts.render("translator_system", src="ja", tgt="en")

        self.assertIn('"translations"', rendered)
        self.assertIn("equally long array of English translations", rendered)
        self.assertIn("standard English typography", rendered)

    def test_glossary_agent_contract_uses_target_language_categories_and_notes(self):
        for name in ("analyzer_system", "glossary_extractor_system"):
            with self.subTest(prompt=name):
                rendered = prompts.render(name, src="ja", tgt="en")
                self.assertIn("person, place, organization", rendered)
                self.assertIn("male, female, or unknown", rendered)
                self.assertIn("note", rendered)
                self.assertIn("English", rendered)
                self.assertNotIn('"gender":"男/女/未知', rendered)
                self.assertNotIn('"type":"人物/地名', rendered)

    def test_glossary_metadata_is_localized_without_translating_reading(self):
        english_term = GlossaryTerm(
            source="堀北",
            target="Horikita",
            reading="ほりきた",
            type="person",
            gender="female",
            aliases=["堀北さん"],
            note="A female student.",
        )
        chinese_term = GlossaryTerm(
            source="堀北",
            target="堀北",
            reading="ほりきた",
            type="人物",
            gender="女",
            aliases=["堀北さん"],
            note="女学生。",
        )

        english = prompts.render_glossary([english_term], tgt="en")
        chinese = prompts.render_glossary([chinese_term], tgt="zh")

        self.assertIn("(person, female, reading: ほりきた)", english)
        self.assertIn("[aliases: 堀北さん]", english)
        self.assertIn("[note: A female student.]", english)
        self.assertNotIn("人物", english)
        self.assertNotIn("女，", english)
        self.assertIn("（人物，女，读音: ほりきた）", chinese)
        self.assertIn("[别名: 堀北さん]", chinese)

    def test_existing_glossary_metadata_is_not_cleaned_or_hidden(self):
        term = GlossaryTerm(
            source="モーターサイクルの男",
            target="the Motorcycle Man",
            type="称谓",
            gender="男",
            note="称呼变体，指代 the man",
        )

        rendered = prompts.render_glossary([term], tgt="en")

        self.assertIn("称谓", rendered)
        self.assertIn("男", rendered)
        self.assertIn("称呼变体，指代 the man", rendered)

    def test_dynamic_english_formatters_are_localized(self):
        brief = language.style_brief(
            {
                "genre": "literary fiction",
                "tone": "restrained",
                "characters": [],
            },
            [],
            target="en",
        )
        sample = language.sample_block(
            language.sample_labels("en")[0],
            "sample text",
            target="en",
        )
        feedback = language.autofix_feedback(
            [{"detail": "A sentence was omitted.", "suggestion": "Restore it."}],
            target="en",
        )

        self.assertIn("Genre: literary fiction", brief)
        self.assertIn("Tone and style: restrained", brief)
        self.assertEqual(sample, "[Opening sample]\nsample text")
        self.assertIn("Suggested correction: Restore it.", feedback)

    def test_english_punctuation_normalizes_quotes_only(self):
        original = (
            "He said, “Wait…”, then replied, ‘I won’t.’ "
            "「Again」 『inside』 ＂wide＂ ＇apostrophe＇ "
            "«angle» ‹nested› — done."
        )

        self.assertTrue(language.punctuation_enabled("en"))
        self.assertEqual(
            language.normalize_punctuation(original, target="en"),
            'He said, "Wait…", then replied, \'I won\'t.\' '
            '"Again" \'inside\' "wide" \'apostrophe\' '
            '"angle" \'nested\' — done.',
        )

    def test_english_ascii_quotes_are_unchanged(self):
        original = 'He said, "Wait...", then replied, \'I won\'t.\''

        self.assertEqual(
            language.normalize_punctuation(original, target="en"),
            original,
        )

    def test_generic_non_english_target_does_not_reuse_english_normalizer(self):
        original = "Il dit : « “bonjour” »."

        self.assertFalse(language.punctuation_enabled("fr"))
        self.assertEqual(
            language.normalize_punctuation(original, target="fr"),
            original,
        )


class TestEnglishTargetEndToEnd(unittest.TestCase):
    def test_japanese_novel_runs_through_every_stage_into_english(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            write_sample_txt(source)
            config = Config.from_dict(
                {
                    "language": {"source": "ja", "target": "en"},
                    "llm": {"provider": "fake"},
                    "pipeline": {
                        "review": True,
                        "polish": True,
                        "backtranslate_sample": 1.0,
                        "consistency_qa": True,
                        "book_understanding": True,
                        "prescan_concurrency": 1,
                        "review_concurrency": 1,
                    },
                    "paths": {"state_dir": os.path.join(directory, "state")},
                    "output": {"about_page": True},
                }
            )

            client = FakeClient(handler=routing_handler)
            result = Orchestrator(config, client=client).run_all(
                source,
                out_format="epub",
            )
            store = result["store"]
            manifest = store.load_manifest()

            self.assertTrue(store.run_dir.endswith("novel@en"))
            self.assertEqual(manifest["source_lang"], "ja")
            self.assertEqual(manifest["target_lang"], "en")
            self.assertTrue(
                all(
                    chapter.get("title_translated", "").startswith("Chapter ")
                    for chapter in manifest["chapters"]
                )
            )

            targets: list[str] = []
            digests: list[str] = []
            for item in manifest["chapters"]:
                chapter = store.load_chapter(item["index"])
                targets.extend(segment.target or "" for segment in chapter.text_segments)
                digests.append(chapter.meta.get("source_digest", ""))
            self.assertTrue(targets)
            self.assertTrue(all(target.startswith('He said, "Hello ') for target in targets))
            self.assertFalse(any("，" in target or "。" in target for target in targets))
            self.assertTrue(all("plot advances" in digest for digest in digests))

            analysis = store.load_analysis() or {}
            self.assertEqual(analysis.get("style_guide"), "Use restrained literary English.")
            self.assertIn("main plot", analysis.get("book_synopsis", ""))

            glossary = GlossaryStore(store.glossary_path)
            try:
                glossary_terms = glossary.all_terms()
            finally:
                glossary.close()
            glossary_targets = {term.target for term in glossary_terms}
            self.assertIn("Ayanokoji", glossary_targets)
            self.assertIn("Horikita", glossary_targets)
            self.assertTrue(all(term.type in {"person", "term"}
                                for term in glossary_terms))
            self.assertTrue(all(term.gender in {"", "male", "female", "unknown"}
                                for term in glossary_terms))
            self.assertTrue(
                all(not term.note or term.note.endswith(".") for term in glossary_terms)
            )

            output = result["output"]
            self.assertTrue(output.endswith(".en.epub"))
            with zipfile.ZipFile(output) as archive:
                contents = [archive.read(name) for name in archive.namelist()]
            joined = b"\n".join(contents).decode("utf-8", errors="ignore")
            self.assertIn("<dc:language>en</dc:language>", joined)
            self.assertIn("About This Translation", joined)
            self.assertNotIn("翻译为中文", joined)

            stages = {call["stage"] for call in client.calls}
            self.assertTrue(
                {
                    "Analyzer",
                    "Synopsizer",
                    "Translator",
                    "Polisher",
                    "Reviewer",
                    "BackTranslator",
                    "GlossaryExtractor",
                    "ConsistencyChecker",
                }.issubset(stages)
            )
            reviewer_systems = [
                call["messages"][0]["content"]
                for call in client.calls
                if call["stage"] == "Reviewer"
            ]
            backtranslation_systems = [
                call["messages"][0]["content"]
                for call in client.calls
                if call["stage"] == "BackTranslator"
                and "backtranslator" in call["messages"][0]["content"]
            ]
            self.assertTrue(
                any(
                    "Japanese source paragraph" in text
                    and "English translation" in text
                    for text in reviewer_systems
                )
            )
            self.assertTrue(
                any(
                    "English translation back into Japanese" in text
                    for text in backtranslation_systems
                )
            )
            model_inputs = "\n".join(
                str(message.get("content", ""))
                for call in client.calls
                for message in call["messages"]
            )
            for marker in (
                "【",
                "（无）",
                "原文：",
                "译文：",
                "回译：",
                "建议：",
                "体裁：",
                "风格指南：",
                "开头样章",
                "中部样章",
                "结尾样章",
            ):
                with self.subTest(chinese_prompt_marker=marker):
                    self.assertNotIn(marker, model_inputs)


if __name__ == "__main__":
    unittest.main()
