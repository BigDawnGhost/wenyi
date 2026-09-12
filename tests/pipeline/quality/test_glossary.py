"""术语表 AI 审计测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.fixtures.books import write_sample_txt
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.ingest.models import Chapter, ChapterProcessing, Segment
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application
from trans_novel.pipeline.state import (
    ChapterIndex,
    ChapterProgress,
    RunIdentity,
    RunState,
    RunStore,
)


class TestGlossaryAudit(unittest.TestCase):
    def test_unify_variants_and_rewrite_targets(self):
        from trans_novel.agents.glossary_auditor import GlossaryAuditor
        from trans_novel.pipeline.quality import audit_glossary

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
            cfg.source_lang = "ja"
            cfg.state_dir = state
            orch = Application(cfg, client=FakeClient(handler=routing_handler))
            store = orch.run(txt)

            # 人为制造译法漂移：术语表写入 佳穂子，章节正文里混入 佳穗子（3字，避开防线2的2字上限）
            g = GlossaryStore(store.glossary_path)
            g.upsert_term(GlossaryTerm(source="カホ", target="佳穂子", type="人物"), chapter=0)
            g.close()
            ch = store.load_chapter(0)
            ch.segments[1].target = "佳穂子和佳穗子在一起。"  # 同名两种写法
            store.save_chapter(ch)

            def handler(messages, agent, operation, json_mode):
                if "术语一致性审计员" in messages[0]["content"]:
                    return json.dumps(
                        {
                            "unifications": [
                                {
                                    "source": "カホ",
                                    "canonical": "佳穂子",
                                    "variants": ["佳穗子"],
                                    "reason": "统一为佳穂子",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                return "{}"

            g = GlossaryStore(store.glossary_path)
            client = FakeClient(handler=handler)
            applied = audit_glossary(store, g, GlossaryAuditor(client, cfg))
            self.assertEqual(client.calls[0]["agent"], "analyst")
            self.assertEqual(client.calls[0]["operation"], "glossary.audit")
            self.assertEqual(len(applied), 1)
            term = g.get_term("カホ")
            self.assertTrue(term.locked)
            self.assertIn("佳穗子", term.aliases)
            g.close()

            # 正文里的 佳穗子 应已被改写为 佳穂子
            ch2 = store.load_chapter(0)
            self.assertEqual(ch2.segments[1].target, "佳穂子和佳穂子在一起。")
            # glossary_rewrite_applied 不复述全局 replace_map、不带 source 明文，
            # 只带实际命中的替换元数据与 before/after
            with open(store.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            rewrites = [e for e in events if e["event"] == "glossary_rewrite_applied"]
            self.assertTrue(rewrites, "正文改写应发 glossary_rewrite_applied")
            for e in rewrites:
                self.assertNotIn("source", e, "事件不带 source 明文")
                self.assertNotIn("replace_map", e, "事件不复述全局 replace_map")
                self.assertIn("before", e)
                self.assertIn("after", e)
                self.assertEqual(e["replacements"], [{"variant": "佳穗子", "canonical": "佳穂子"}])

    def test_rewrite_replacements_follow_actual_sequential_execution(self):
        """glossary_rewrite_applied 的 replacements 必须来自真实替换过程：
        重叠变体被先执行的长变体吞掉时不误报；后执行替换引入的新变体随后
        命中并执行时如实按序上报；同一章多段改写只整章保存一次。"""
        from trans_novel.pipeline.quality import rewrite_targets

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "state"))
            store.save_state(
                RunState(
                    identity=RunIdentity(
                        source_bytes_sha256="test-hash",
                        run_input_schema_version=2,
                        source_lang="en",
                        target_lang="zh",
                    ),
                    title="T",
                    fmt="text",
                    source_path="",
                    source_lang="en",
                    target_lang="zh",
                    chapters=[ChapterIndex(index=0, title="第一章", href=None)],
                    progress={0: ChapterProgress()},
                )
            )
            chapter = Chapter(
                index=0,
                title="第一章",
                segments=[
                    # 段0：BC 被先执行的长变体 ABC 整段吞掉 → 只报 ABC
                    Segment(index=0, source="a", target="内容ABC结尾。"),
                    # 段1：AB→CD 引入了变体 CD，随后 CD→E 真实执行 → 按序报 AB、CD
                    Segment(index=1, source="b", target="前缀AB后缀。"),
                    Segment(index=2, source="c", target="无变体段落。"),
                ],
            )
            store.save_chapter(chapter)
            g = GlossaryStore(store.glossary_path)
            saves: list[int] = []
            real_save = store.save_chapter

            def counting_save(ch):
                saves.append(ch.index)
                return real_save(ch)

            store.save_chapter = counting_save
            changed = rewrite_targets(store, g, {"ABC": "X", "BC": "Y", "AB": "CD", "CD": "E"})
            g.close()

            self.assertEqual(changed, 2)
            self.assertEqual(saves, [0], "同一章的多段改写应只整章保存一次")
            reloaded = store.load_chapter(0)
            self.assertEqual(reloaded.segments[0].target, "内容X结尾。")
            self.assertEqual(reloaded.segments[1].target, "前缀E后缀。")
            self.assertEqual(reloaded.segments[2].target, "无变体段落。")
            with open(store.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            rewrites = {e["index"]: e for e in events if e["event"] == "glossary_rewrite_applied"}
            self.assertEqual(
                rewrites[0]["replacements"],
                [{"variant": "ABC", "canonical": "X"}],
                "被长变体吞掉的 BC 不得误报",
            )
            self.assertEqual(
                rewrites[1]["replacements"],
                [{"variant": "AB", "canonical": "CD"}, {"variant": "CD", "canonical": "E"}],
                "替换引入的新变体随后执行时应按序上报",
            )

    def test_latin_residue_coalesces_one_save_per_chapter(self):
        """多个锁定拉丁术语命中同一章：每章只保存一次、每个命中术语一条 applied 记录；
        glossary_latin_residue_fixed 事件紧凑（无 source 明文）且按章落盘后发出。"""
        from trans_novel.agents.glossary_auditor import GlossaryAuditor
        from trans_novel.pipeline.quality import audit_glossary

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "state"))
            store.save_state(
                RunState(
                    identity=RunIdentity(
                        source_bytes_sha256="test-hash",
                        run_input_schema_version=2,
                        source_lang="en",
                        target_lang="zh",
                    ),
                    title="T",
                    fmt="text",
                    source_path="",
                    source_lang="en",
                    target_lang="zh",
                    chapters=[ChapterIndex(index=0, title="第一章", href=None)],
                    progress={0: ChapterProgress()},
                )
            )
            chapter = Chapter(
                index=0,
                title="第一章",
                segments=[
                    Segment(index=0, source="a", target="Ayanokoji 走进了教室。"),
                    Segment(index=1, source="b", target="Kaho 笑了笑。Ayanokoji 点头。"),
                ],
            )
            store.save_chapter(chapter)
            g = GlossaryStore(store.glossary_path)
            g.upsert_term(
                GlossaryTerm(source="Kaho", target="佳穂", confidence="high", locked=True)
            )
            g.upsert_term(
                GlossaryTerm(source="Ayanokoji", target="綾小路", confidence="high", locked=True)
            )

            cfg = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
            cfg.source_lang = "en"
            cfg.state_dir = os.path.join(d, "state")
            client = FakeClient(handler=lambda m, a, o, j: "{}")

            saves: list[int] = []
            real_save = store.save_chapter

            def counting_save(ch):
                saves.append(ch.index)
                return real_save(ch)

            store.save_chapter = counting_save
            applied = audit_glossary(store, g, GlossaryAuditor(client, cfg))
            g.close()

            self.assertEqual(saves, [0], "多个术语命中同一章应只整章写一次")
            reloaded = store.load_chapter(0)
            self.assertEqual(reloaded.segments[0].target, "綾小路走进了教室。")
            self.assertEqual(reloaded.segments[1].target, "佳穂笑了笑。綾小路点头。")
            self.assertEqual(len(applied), 2, "每个命中术语一条 applied 记录")
            self.assertEqual({a["source"] for a in applied}, {"Kaho", "Ayanokoji"})
            with open(store.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            fixed = [e for e in events if e["event"] == "glossary_latin_residue_fixed"]
            self.assertEqual(len(fixed), 3)  # 段0 一处 + 段1 两处
            for e in fixed:
                self.assertNotIn("source", e, "事件不带 source 明文")
                self.assertNotIn("replace_map", e)
                self.assertIn("before", e)
                self.assertIn("after", e)
                self.assertIn("term_source", e)
                self.assertIn("term_target", e)

    def test_preserved_chapters_are_immune_to_all_glossary_rewrites(self):
        from trans_novel.pipeline.quality import fix_latin_residue, rewrite_targets

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(os.path.join(directory, "state"))
            processing = ChapterProcessing(
                action="preserve",
                review_required=False,
                reason="reference list",
                source_sha256="source",
                strategy_version="chapter_semantics_v1",
            )
            store.save_state(
                RunState(
                    identity=RunIdentity(source_bytes_sha256="test-hash"),
                    title="T",
                    fmt="text",
                    source_lang="en",
                    target_lang="zh",
                    chapters=[ChapterIndex(index=0, title="Sources", processing=processing)],
                    progress={0: ChapterProgress()},
                )
            )
            chapter = Chapter(
                index=0,
                title="Sources",
                segments=[Segment(index=0, source="Smith Kaho", target="Smith Kaho 中文")],
                processing=processing,
            )
            store.save_chapter(chapter)
            glossary = GlossaryStore(store.glossary_path)
            glossary.upsert_term(
                GlossaryTerm(source="Kaho", target="佳穂", confidence="high", locked=True)
            )

            self.assertEqual(rewrite_targets(store, glossary, {"Smith": "史密斯"}), 0)
            self.assertEqual(fix_latin_residue(store, glossary), [])
            self.assertEqual(store.load_chapter(0).segments[0].target, "Smith Kaho 中文")
            glossary.close()


if __name__ == "__main__":
    unittest.main()
