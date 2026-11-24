"""Glossary trap cases from the volume-1 benchmark of a Chinese court novel (zh→en).

Each case reproduces a wrong mapping observed in a real run: 孤 stored as "lone" from 孤舟,
the place 横江 stored as "lay across the river" from 白露横江, forms of address with a single
book-wide rendering, one-off entries, and conflicts that differ only by an article.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from wenyi_core.agents.glossary_extractor import GlossaryExtractor, TranslatedSegmentEvidence
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm, equivalent_targets
from wenyi_core.i18n.languages import (
    glossary_context_dependent_sources,
    glossary_single_character_names_only,
)
from wenyi_core.llm.providers.fake import FakeClient


def _config(source: str = "zh", target: str = "en") -> Config:
    return Config.from_dict(
        {"language": {"source": source, "target": target}, "llm": {"preset": "fake"}}
    )


def _extractor(
    extracted: list[dict],
    aligned: list[dict] | None = None,
    source: str = "zh",
    target: str = "en",
):
    calls: list[str] = []

    # Appellation-like types are admitted only with an explicit stable scope.
    extracted = [{"scope": "stable", **item} for item in extracted]

    def handler(messages, tier, json_mode):
        system = messages[0]["content"]
        calls.append(system)
        if "terminology consistency aligner" in system:
            return json.dumps({"terms": aligned or []}, ensure_ascii=False)
        return json.dumps({"terms": extracted}, ensure_ascii=False)

    return GlossaryExtractor(FakeClient(handler=handler), _config(source, target)), calls


class TestContextDependentSources(unittest.TestCase):
    def test_profiles_list_chinese_forms_of_address_for_both_scripts(self):
        simplified = glossary_context_dependent_sources("zh")
        traditional = glossary_context_dependent_sources("zh-Hant")
        self.assertTrue({"孤", "本王", "大人", "公子", "先生", "下官"} <= simplified)
        self.assertTrue({"孤", "閣下", "老爺"} <= traditional)
        self.assertEqual(glossary_context_dependent_sources("ja"), frozenset())
        self.assertEqual(glossary_context_dependent_sources("auto"), frozenset())
        self.assertTrue(glossary_single_character_names_only("zh-Hant"))
        self.assertFalse(glossary_single_character_names_only("ja"))

    def test_self_references_and_forms_of_address_are_not_extracted(self):
        extractor, _ = _extractor(
            [
                {"source": "孤", "target": "lone", "type": "appellation"},
                {"source": "本王", "target": "my", "type": "appellation"},
                {"source": "大人", "target": "Your Excellency", "type": "honorific"},
                {"source": "李贽", "target": "Li Zhi", "type": "person"},
                {"source": "李大人", "target": "Lord Li", "type": "appellation"},
            ]
        )
        result = extractor.extract("孤与本王问李大人，李贽笑道：大人请。", "Text.", [])
        self.assertEqual([t.source for t in result], ["李贽", "李大人"])

    def test_forms_of_address_are_removed_from_aliases(self):
        extractor, _ = _extractor(
            [
                {
                    "source": "宁亲王",
                    "target": "Prince Ning",
                    "type": "person",
                    "aliases": ["本王", "沈衡", "王爷"],
                }
            ]
        )
        result = extractor.extract("宁亲王沈衡道：本王来了。", "Text.", [])
        self.assertEqual(result[0].aliases, ["沈衡"])

    def test_single_character_common_words_are_dropped_but_names_kept(self):
        payload = [
            {"source": "课", "target": "reading", "type": "term"},
            {"source": "丈", "target": "zhang", "type": "item"},
            {"source": "蜀", "target": "Shu", "type": "place"},
            {"source": "胜", "target": "Sheng", "type": "person"},
            {"source": "晋", "target": "Jin", "type": "setting"},
        ]
        extractor, _ = _extractor(payload)
        result = extractor.extract("课上说蜀国主胜身高一丈，晋人亦然。", "Text.", [])
        self.assertEqual([t.source for t in result], ["蜀", "胜", "晋"])

        japanese, _ = _extractor(
            [{"source": "術", "target": "术", "type": "technique"}], source="ja"
        )
        self.assertEqual([t.source for t in japanese.extract("術を使う。", "使用术。", [])], ["術"])


class TestHistoryAlignmentTraps(unittest.TestCase):
    def _store_once(self, extractor, source, target, history, corpus=None):
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = extractor.extract_and_store(
                store,
                source,
                target,
                chapter=6,
                history=history,
                before=(6, 0),
                source_corpus=corpus,
            )
            terms = {t.source: t for t in store.all_terms()}
            store.close()
        return summary, terms

    def test_single_character_name_is_not_aligned_to_a_longer_word(self):
        extractor, calls = _extractor([{"source": "胜", "target": "Sheng", "type": "person"}])
        history = [
            TranslatedSegmentEvidence(0, 2, "胜利在望，众人欢呼。", "Victory was in sight."),
        ]
        summary, terms = self._store_once(
            extractor, "国主胜微恙。", "The sovereign, Sheng, was unwell.", history
        )
        self.assertEqual(terms["胜"].target, "Sheng")
        self.assertEqual(terms["胜"].first_chapter, 6)
        self.assertEqual(summary["history_matched"], 0)
        self.assertEqual(len(calls), 1)

    def test_name_aligned_to_lowercase_wording_keeps_the_batch_translation(self):
        extractor, calls = _extractor(
            [{"source": "横江", "target": "Hengjiang", "type": "place"}],
            aligned=[{"source": "横江", "target": "lay across the river"}],
        )
        history = [
            TranslatedSegmentEvidence(
                0,
                3,
                "白露横江，水光接天。",
                "White dew lay across the river; the water met the sky.",
            ),
        ]
        with self.assertLogs("wenyi_core.agents.glossary_extractor", level="WARNING"):
            summary, terms = self._store_once(
                extractor, "安亲王至横江。", "Prince An went to Hengjiang.", history
            )
        self.assertEqual(terms["横江"].target, "Hengjiang")
        self.assertEqual(terms["横江"].first_chapter, 6)
        self.assertEqual(summary["history_aligned"], 0)
        self.assertEqual(summary["history_matched"], 0)
        self.assertEqual(len(calls), 2)

    def test_caseless_historical_rendering_is_still_aligned(self):
        extractor, _ = _extractor(
            [{"source": "Class C", "target": "C班", "type": "organization"}],
            aligned=[{"source": "Class C", "target": "丙班"}],
            source="en",
            target="zh",
        )
        history = [TranslatedSegmentEvidence(1, 0, "Class C met.", "丙班集合了。")]
        summary, terms = self._store_once(extractor, "Class C left.", "C班离开了。", history)
        self.assertEqual(terms["Class C"].target, "丙班")
        self.assertEqual(summary["history_aligned"], 1)

    def test_name_aligned_to_an_earlier_capitalized_rendering_still_aligns(self):
        extractor, _ = _extractor(
            [{"source": "林远", "target": "Lin Yuen", "type": "person"}],
            aligned=[{"source": "林远", "target": "Lin Yuan"}],
        )
        history = [TranslatedSegmentEvidence(1, 0, "林远到了。", "Lin Yuan arrived.")]
        summary, terms = self._store_once(extractor, "林远笑了。", "Lin Yuen smiled.", history)
        self.assertEqual(terms["林远"].target, "Lin Yuan")
        self.assertEqual(terms["林远"].first_chapter, 1)
        self.assertEqual(summary["history_aligned"], 1)


class TestSingleOccurrenceTerms(unittest.TestCase):
    def test_new_terms_that_never_recur_in_the_book_are_not_stored(self):
        extractor, _ = _extractor(
            [
                {"source": "林远", "target": "Lin Yuan", "type": "person"},
                {"source": "刘祯", "target": "Liu Zhen", "type": "person"},
            ]
        )
        corpus = "刘祯送来一幅画。林远看了。\n后来林远又来了。"
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = extractor.extract_and_store(
                store, "刘祯送来一幅画。林远看了。", "Text.", chapter=0, source_corpus=corpus
            )
            self.assertIsNotNone(store.get_term("林远"))
            self.assertIsNone(store.get_term("刘祯"))
            self.assertEqual(summary["inserted"], 1)
            store.close()

    def test_repeats_of_stored_terms_still_reach_the_store(self):
        extractor, _ = _extractor([{"source": "刘祯", "target": "Liu Chen", "type": "person"}])
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            store.upsert_term(GlossaryTerm(source="刘祯", target="Liu Zhen", type="person"))
            summary = extractor.extract_and_store(
                store, "刘祯来了。", "Liu Chen came.", chapter=1, source_corpus="刘祯来了。"
            )
            self.assertEqual(summary["conflict"], 1)
            self.assertEqual(len(store.open_conflicts()), 1)
            store.close()

    def test_without_a_corpus_single_occurrence_terms_are_kept(self):
        extractor, _ = _extractor([{"source": "刘祯", "target": "Liu Zhen", "type": "person"}])
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            extractor.extract_and_store(store, "刘祯来了。", "Liu Zhen came.", chapter=0)
            self.assertIsNotNone(store.get_term("刘祯"))
            store.close()


class TestEquivalentTargets(unittest.TestCase):
    def test_article_case_spacing_and_final_stop_are_equivalent(self):
        for left, right in [
            ("Qingyuan Collection", "the Qingyuan Collection"),
            ("The master", "Master"),
            ("Rise, all of you.", "Rise, all of you"),
            ("six  rites", "Six rites"),
        ]:
            with self.subTest(left=left, right=right):
                self.assertTrue(equivalent_targets(left, right))
        for left, right in [
            ("Prince Ning", "Prince of Ning"),
            ("second place", "second-place laureate"),
            ("", " "),
            ("Theodore", "odore"),
            ("An Lushan", "Lushan"),
            ("A Zi", "Zi"),
        ]:
            with self.subTest(left=left, right=right):
                self.assertFalse(equivalent_targets(left, right))

    def test_extraction_keeps_the_stored_target_without_logging_a_conflict(self):
        extractor, _ = _extractor(
            [
                {
                    "source": "清远集",
                    "target": "the Qingyuan Collection",
                    "type": "item",
                    "aliases": ["清远"],
                }
            ]
        )
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            store.upsert_term(GlossaryTerm(source="清远集", target="Qingyuan Collection"))
            summary = extractor.extract_and_store(
                store, "他读清远集。", "He read the Qingyuan Collection.", chapter=1
            )
            term = store.get_term("清远集")
            assert term is not None
            self.assertEqual(summary["unchanged"], 1)
            self.assertEqual(term.target, "Qingyuan Collection")
            self.assertEqual(term.aliases, ["清远"])
            self.assertEqual(term.status, "ok")
            self.assertEqual(store.open_conflicts(), [])
            store.close()

    def test_direct_store_writes_still_compare_targets_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            store.upsert_term(GlossaryTerm(source="清远集", target="qingyuan collection"))
            self.assertEqual(
                store.upsert_term(GlossaryTerm(source="清远集", target="Qingyuan Collection")),
                "conflict",
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
