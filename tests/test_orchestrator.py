"""ç¼–æŽ’å™¨ç«¯åˆ°ç«¯ + æ–­ç‚¹ç»­è·‘æµ‹è¯•ï¼ˆç¦»çº¿ FakeClientï¼‰ã€‚"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt
from trans_novel.agents.reviewer import ReviewOutputError
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.usage import UsageSample
from trans_novel.pipeline.orchestrator import Orchestrator, _normalize_lang
from trans_novel.pipeline.runstore import STATUS_DONE, STATUS_PENDING, RunStore


def _translated_para_count(calls) -> int:
    """ç»Ÿè®¡é€è¿›ç¿»è¯‘æ¨¡åž‹çš„æºæ®µæ€»æ•°ï¼ˆæŒ‰ç¼–å·è¡Œè®¡ï¼‰ã€‚"""
    n = 0
    for c in calls:
        if "æ–‡å­¦ç¿»è¯‘" in c["messages"][0]["content"]:
            n += len(re.findall(r"^\[(\d+)\]", c["messages"][-1]["content"], re.MULTILINE))
    return n


def _review_json(user: str, issues: list[dict]) -> str:
    """æž„é€ å¸¦å®Œæ•´æ€§å›žæ‰§çš„ Reviewer æµ‹è¯•å“åº”ã€‚"""
    return json.dumps(
        {
            "issues": issues,
            "reviewed_segments": len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE)),
            "complete": True,
        },
        ensure_ascii=False,
    )


def _fix_json(user: str, replacement: str) -> str:
    """ä»Ž Fixer è¯·æ±‚å›žæ˜¾èº«ä»½å­—æ®µï¼Œå¹¶æž„é€ å®Œæ•´ä¸´æ—¶æ›¿æ¢åè®®ã€‚"""

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


def _config(state_dir: str):
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
            },
            "segment": {"max_chars_per_batch": 1800},
            "pipeline": {
                "review": True,
                "polish": True,
                "backtranslate_sample": 0.0,
                "consistency_qa": True,
            },
            "paths": {"state_dir": state_dir},
        }
    )


class MeteredFakeClient(FakeClient):
    """æ¯æ¬¡ç¦»çº¿è°ƒç”¨éƒ½è®°å½•ä¸€å°ç¬”ç”¨é‡ï¼Œç”¨äºŽéªŒè¯ Review ç”¨é‡éš”ç¦»ã€‚"""

    def complete(
        self,
        messages,
        *,
        tier="strong",
        json_mode=False,
        max_tokens=None,
        stage=None,
    ):
        self.usage.record(
            tier,
            UsageSample(
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
                cache_miss_tokens=5,
            ),
            stage,
        )
        return super().complete(
            messages,
            tier=tier,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )


class TestOrchestrator(unittest.TestCase):
    def test_annotation_alignment_merges_continuations_and_persists_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            cfg.pipeline.annotation_alignment = True

            def handler(messages, tier, json_mode):
                if "align EPUB annotation markers" in messages[0]["content"]:
                    self.assertEqual(tier, "cheap")
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "unit_id": "ch0:tn0_0",
                                    "marked_target": "é˜¿å°”æ³•âŸªtn0_0_annotation_0âŸ« è´å¡”",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="Alpha ",
                        target="é˜¿å°”æ³• ",
                        anchor="tn0_0",
                        meta={
                            "epub_annotations": {
                                "version": 1,
                                "source_length": len("Alpha beta"),
                                "items": [
                                    {
                                        "id": "tn0_0_annotation_0",
                                        "mode": "point",
                                        "source_start": 5,
                                        "source_end": 5,
                                        "source_text": "",
                                        "marker_text": "1",
                                    }
                                ],
                            }
                        },
                    ),
                    Segment(index=1, source="beta", target="è´å¡”", cont=True),
                ],
            )
            store = RunStore(os.path.join(directory, "state", "book"))
            client = FakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)

            orch._align_annotations_after_batch(
                0,
                chapter,
                0,
                2,
                store,
            )

            saved = store.load_chapter(0)
            metadata = saved.segments[0].meta["epub_annotations"]
            self.assertEqual(metadata["placements"][0]["target_start"], len("é˜¿å°”æ³•"))
            self.assertEqual(metadata["placements"][0]["target_end"], len("é˜¿å°”æ³•"))
            self.assertEqual(metadata["placements"][0]["status"], "aligned")
            self.assertTrue(metadata["target_digest"])
            calls = [
                call
                for call in client.calls
                if "align EPUB annotation markers" in call["messages"][0]["content"]
            ]
            self.assertEqual(len(calls), 1)
            annotation_stage = orch._eta._stages["annotation:0:0"]
            self.assertTrue(annotation_stage.finished)
            self.assertEqual(annotation_stage.kind, "chars")
            self.assertEqual(annotation_stage.tier, "cheap")

    def test_annotation_alignment_waits_for_final_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            cfg.pipeline.annotation_alignment = True
            requested: list[str] = []

            def handler(messages, tier, json_mode):
                if "align EPUB annotation markers" in messages[0]["content"]:
                    requested.append(messages[-1]["content"])
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "unit_id": "ch0:tn0_0",
                                    "marked_target": "ç”²âŸªtn0_0_annotation_0âŸ«ä¹™",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="Alpha ",
                        target="ç”²",
                        anchor="tn0_0",
                        meta={
                            "epub_annotations": {
                                "version": 1,
                                "source_length": len("Alpha beta"),
                                "items": [
                                    {
                                        "id": "tn0_0_annotation_0",
                                        "mode": "point",
                                        "source_start": 5,
                                        "source_end": 5,
                                        "source_text": "",
                                        "marker_text": "1",
                                    }
                                ],
                            }
                        },
                    ),
                    Segment(index=1, source="beta", target=None, cont=True),
                ],
            )
            store = RunStore(os.path.join(directory, "state", "book"))
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))

            orch._align_annotations_after_batch(0, chapter, 0, 1, store)
            self.assertEqual(requested, [])

            chapter.segments[1].target = "ä¹™"
            orch._align_annotations_after_batch(0, chapter, 1, 1, store)

            self.assertEqual(len(requested), 1)
            self.assertIn('"immutable_target": "ç”²ä¹™"', requested[0])
            saved = store.load_chapter(0)
            self.assertEqual(
                saved.segments[0].meta["epub_annotations"]["placements"][0]["target_start"],
                1,
            )

    def test_annotation_alignment_processes_multiple_segments_sequentially(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            cfg.pipeline.annotation_alignment = True
            requested_units: list[str] = []

            def annotation_meta(annotation_id: str) -> dict:
                return {
                    "epub_annotations": {
                        "version": 1,
                        "source_length": 1,
                        "items": [
                            {
                                "id": annotation_id,
                                "mode": "point",
                                "source_start": 1,
                                "source_end": 1,
                                "source_text": "",
                                "marker_text": "1",
                            }
                        ],
                    }
                }

            def handler(messages, tier, json_mode):
                if "align EPUB annotation markers" not in messages[0]["content"]:
                    return routing_handler(messages, tier, json_mode)
                user = messages[-1]["content"]
                if '"unit_id": "ch0:a"' in user:
                    unit_id, target, annotation_id = "ch0:a", "ç”²", "a_note"
                else:
                    unit_id, target, annotation_id = "ch0:b", "ä¹™", "b_note"
                requested_units.append(unit_id)
                return json.dumps(
                    {
                        "items": [
                            {
                                "unit_id": unit_id,
                                "marked_target": f"{target}âŸª{annotation_id}âŸ«",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="A",
                        target="ç”²",
                        anchor="a",
                        meta=annotation_meta("a_note"),
                    ),
                    Segment(
                        index=1,
                        source="B",
                        target="ä¹™",
                        anchor="b",
                        meta=annotation_meta("b_note"),
                    ),
                ],
            )
            store = RunStore(os.path.join(directory, "state", "book"))
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))

            orch._align_annotations_after_batch(0, chapter, 0, 2, store)

            self.assertEqual(requested_units, ["ch0:a", "ch0:b"])
            saved = store.load_chapter(0)
            for segment in saved.segments:
                self.assertEqual(
                    segment.meta["epub_annotations"]["placements"][0]["target_start"],
                    1,
                )

    def test_prepare_retries_after_analysis_failure(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            def fail_analysis(messages, tier, json_mode):
                raise RuntimeError("temporary model failure")

            with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
                Orchestrator(cfg, client=FakeClient(handler=fail_analysis)).prepare(txt)

            run_dirs = [os.path.join(cfg.state_dir, name) for name in os.listdir(cfg.state_dir)]
            self.assertEqual(len(run_dirs), 1)
            self.assertFalse(os.path.isfile(os.path.join(run_dirs[0], "manifest.json")))

            store = Orchestrator(cfg, client=FakeClient(handler=routing_handler)).prepare(txt)
            self.assertTrue(store.exists())
            self.assertTrue(store.load_manifest()["initialized"])
            self.assertIsNotNone(store.load_analysis())

    def test_full_run_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)

            # å…¨éƒ¨ç« èŠ‚æ ‡è®° done
            m = store.load_manifest()
            self.assertEqual(len(m["chapters"]), 2)
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m["chapters"]))

            # æ¯æ®µéƒ½æœ‰è¯‘æ–‡ï¼ˆæ¶¦è‰²åŽä¸º "æ¶¦{i}"ï¼‰
            ch0 = store.load_chapter(0)
            self.assertTrue(all(s.target for s in ch0.text_segmentsÛ­6ÖÚ$z{-®éÜj×‚"Ð¢6×ÆRÒ÷&6†W7G&F÷"å÷6×ÆU÷FW‡B†Fö2Ð¢6VÆbæ76W'DWVÂ‡6×ÆRæ6÷VçB‚.8	[ÈZKNj~zº8	"’ÂÐ¢6VÆbæ76W'Dæ÷D–â‚.8	KŠÞ˜:Žj~zº8	"Â6×ÆRÐ¢6VÆbæ76W'Dæ÷D–â‚.8	{¹>[îj~zº8	"Â6×ÆRÐ Ð¢FVbFW7E÷7G–ÆUö'&–VeöæWuöf–VÆG2‡6VÆb“ Ð¢""'7G–ÆUö'&–Vbk‹.iù>ikš8îjÎ{»N[ªnûÉ¾izræÇ—6—>ûÈŽ{Ë®ikZÙ~jë^ûÈžKˆÞhª^™IžKˆÞ‹é>X{®8""" Ð¢g&öÒG&ç5öæ÷fVÂævVçG2ææÇ—¦W"–×÷'BæÇ—¦W Ð¢g&öÒG&ç5öæ÷fVÂæÆÆÒç&÷f–FW'2æf¶R–×÷'Bf¶T6Æ–VçB2d0Ð Ð¢6frÒö6öæf–r‚'7FFR"Ð¢æÒæÇ—¦W"„d2‚’Â6frÐ¢'&–VbÒæç7G–ÆUö'&–Vb€Ð¢°Ð¢&vVç&R#¢.j
YºÒ"ÀÐ¢'6–ær#¢.yúÞXú^K‹®K‹²"ÀÐ¢'&Vv—7FW"#¢.Xú>ŠúÒ"ÀÐ¢&F–ÆöwVU÷7G–ÆR#¢.ŠúÞk	NŠøÞK‹ZøÂ"ÀÐ¢&æ'&F–öâ#¢.zÊÎKˆK«®z{"ÀÐ¢ÐÐ¢Ð¢6VÆbæ76W'D–â‚.Xú^[Èþˆ¨.ZXþûÉ®yúÞXú^K‹®K‹²"Â'&–VbÐ¢6VÆbæ76W'D–â‚.ŠúÞYùþûÉ®Xú>ŠúÒ"Â'&–VbÐ¢6VÆbæ76W'D–â‚.ZûžŠùÞš8îjÎûÉ®ŠúÞk	NŠøÞK‹ZøÂ"Â'&–VbÐ¢6VÆbæ76W'D–â‚.XùžK¨¾ûÉ®zÊÎKˆK«®z{"Â'&–VbÐ¢2iz~jÎ[ÈþûÉ®Xú®iÈžˆZÙ~jëPÐ¢öÆBÒæç7G–ÆUö'&–Vb‡²&vVç&R#¢.j
YºÒ"Â'FöæR#¢.Xk~[;²'ÒÐ¢6VÆbæ76W'D–â‚.KÙ>Š8ûÉ®j
YºÒ"ÂöÆBÐ¢6VÆbæ76W'Dæ÷D–â‚.Xú^[Èþˆ¨.ZXò"ÂöÆBÐ Ð Ð¦6Æ72FW7DvÆ÷76'•66÷R‡Væ—GFW7BåFW7D66R“ Ð¢FVb÷'Vå÷v—F…÷FW&×2‡6VÆbÂBÂ66÷R“ Ð¢g&öÒG&ç5öæ÷fVÂævÆ÷76'’ç7F÷&R–×÷'BvÆ÷76'•7F÷&RÂvÆ÷76'•FW&ÐÐ Ð¢G‡BÒ÷2çF‚æ¦ö–â†BÂ&æ÷fVÂçG‡B"Ð¢w&—FU÷6×ÆU÷G‡B‡G‡BÐ¢6frÒö6öæf–r†÷2çF‚æ¦ö–â†BÂ'7FFR"’Ð¢6frç—VÆ–æRævÆ÷76'•÷66÷RÒ66÷PÐ Ð¢÷&6‚Ò÷&6†W7G&F÷"†6frÂ6Æ–VçCÔf¶T6Æ–VçB††æFÆW#×&÷WF–æuö†æFÆW"’Ð¢7F÷&RÒ÷&6‚ç&W&R‡G‡BÐ¢rÒvÆ÷76'•7F÷&R‡7F÷&RævÆ÷76'•÷F‚Ð¢2)jÚ>ih~ZInK«®xš’)izX[>iÊþŠúÞûÈ‡6÷W&6RöÆ–2YØ~KˆÞYÊŽjÚ>ih~ûÈž)&Æ–2YÊŽjÚ>ih~X{®xë Ð¢rçW6W'E÷FW&Ò„vÆ÷76'•FW&Ò‡6÷W&6SÒ.ZIn˜:ŽK«®xš•‚"ÂF&vWCÒ.ZIn˜:ŽŠùYÒ"ÂG—SÒ.K«®xš’"’Ð¢rçW6W'E÷FW&Ò„vÆ÷76'•FW&Ò‡6÷W&6SÒ.xJ™j.Kø.yJŽŠ©â"ÂF&vWCÒ.izX[>iÊþŠúÒ"ÂG—SÒ.iÊþŠúÒ"’Ð¢rçW6W'E÷FW&Ò€Ð¢vÆ÷76'•FW&Ò‡6÷W&6SÒ.89¾8:®8*Þ8+ò"ÂF&vWCÒ.ZXÉ~ŠùYÒ"ÂÆ–6W3Õ².ZXÉr%ÒÂG—SÒ.iÊþŠúÒ"Ð¢Ð¢ræ6Æ÷6R‚Ð Ð¢6Æ–VçBÒf¶T6Æ–VçB††æFÆW#×&÷WF–æuö†æFÆW"Ð¢÷&6†W7G&F÷"†6frÂ6Æ–VçCÖ6Æ–VçB’ç'Vâ‡G‡BÐ¢&WGW&â°Ð¢%Æâ"æ¦ö–â†Õ²&6öçFVçB%Òf÷"Ò–â5²&ÖW76vW2%ÒÐ¢f÷"2–â6Æ–VçBæ6ÆÇ0Ð¢–b.ih~ZÚn{û¾Šù"–â5²&ÖW76vW2%Õ³Õ²&6öçFVçB%ÐÐ¢ÐÐ Ð¢FVbFW7Eö6†FW%÷66÷U÷'VæW2‡6VÆb“ Ð¢""&6†FW.ûÉ®jÚ>ih~ZIniÚyºîX™N™šNûÈÆÆ–2YÞKŠÞy¨NiÚyºîKùÞyYž8""" Ð¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2C Ð¢G&ç6ÆFU÷&ö×G2Ò6VÆbå÷'Vå÷v—F…÷FW&×2†BÂ&6†FW""Ð¢6VÆbæ76W'EG'VR‡G&ç6ÆFU÷&ö×G2Ð¢f÷"–âG&ç6ÆFU÷&ö×G3 Ð¢6VÆbæ76W'Dæ÷D–â‚.ZIn˜:ŽK«®xš•‚"Â’2iÊÎzºiÊ®X{®xëûÉ®X™N™š@Ð¢6VÆbæ76W'Dæ÷D–â‚.xJ™j.Kø.yJŽŠ©â"Â’2iÊÎzºiÊ®X{®xëûÉ®X™N™š@Ð¢6VÆbæ76W'D–â‚.89¾8:®8*Þ8+ò"Â’2XŠ¾YÞ8ÎZXÉ~8ÞYÊŽjÚ>ih~ûÉ®KùÞyYÐ Ð¢FVbFW7EögVÆÅ÷66÷Uö¶VW5öÆÂ‡6VÆb“ Ð¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2C Ð¢G&ç6ÆFU÷&ö×G2Ò6VÆbå÷'Vå÷v—F…÷FW&×2†BÂ&gVÆÂ"Ð¢6VÆbæ76W'EG'VR‡G&ç6ÆFU÷&ö×G2Ð¢f÷"–âG&ç6ÆFU÷&ö×G3 Ð¢6VÆbæ76W'D–â‚.ZIn˜:ŽK«®xš•‚"ÂÐ¢6VÆbæ76W'D–â‚.xJ™j.Kø.yJŽŠ©â"ÂÐ¢6VÆbæ76W'D–â‚.89¾8:®8*Þ8+ò"ÂÐ Ð¢FVbFW7Eö&F6…övÆ÷76'•÷&Vg&W6†W5öföÆÆ÷v–æu÷&ö×G2‡6VÆb“ Ð¢"".h›žjÊ{û¾ŠùYîZéîi{nh«ÞXùniÊþŠúÞûÈÎYî{ºÞh›žjÊ&ö×Bz¸¾XÛ>[ŠnKˆ®ikz{‹	>8""" Ð Ð¢FVb†æFÆW"†ÖW76vW2ÂF–W"Â§6öåöÖöFR“ Ð¢7—7FVÒÒÖW76vW5³Õ²&6öçFVçB%ÐÐ¢W6W"ÒÖW76vW5²ÓÕ²&6öçFVçB%ÐÐ¢–b.ih~ZÚn{û¾Šù"–â7—7FVÓ Ð¢âÒÆVâ‡&Ræf–æFÆÂ‡"%åÅ²…ÆB²•ÅÒ"ÂW6W"Â&RäÕTÅD”Ä”äR’Ð¢&WGW&â§6öâæGV×2€Ð¢²'G&ç6ÆF–öç2#¢².[þZHþ[ˆb"f÷"ò–â&ævR†â•×ÒÂVç7W&Uö66–“ÔfÇ6PÐ¢Ð¢–b€Ð¢.iÊþŠúÒ"–â7—7FVÐÐ¢æB.h«ÞXùnYš‚"–â7—7FVÐÐ¢æB.ZHþ[ˆn88(>8)2"–âW6W Ð¢æB.[þZHþ[ˆb"–âW6W Ð¢“ Ð¢&WGW&â§6öâæGV×2€Ð¢°Ð¢'FW&×2#¢°Ð¢°Ð¢'6÷W&6R#¢.ZHþ[ˆn88(>8)2"ÀÐ¢'F&vWB#¢.[þZHþ[ˆb"ÀÐ¢'G—R#¢.z{‹	2"ÀÐ¢&Æ–6W2#¢².ZHþ[ˆb%ÒÀÐ¢&æ÷FR#¢.K«.i‹^z{YÂ"ÀÐ¢ÐÐ¢ÐÐ¢ÒÀÐ¢Vç7W&Uö66–“ÔfÇ6RÀÐ¢Ð¢&WGW&â&÷WF–æuö†æFÆW"†ÖW76vW2ÂF–W"Â§6öåöÖöFRÐ Ð¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2C Ð¢G‡BÒ÷2çF‚æ¦ö–â†BÂ&æ÷fVÂçG‡B"Ð¢v—F‚÷Vâ‡G‡BÂ'r"ÂVæ6öF–æsÒ'WFbÓ‚"’2c Ð¢bçw&—FR€Ð¢"2zÊÎKˆzºÆåÆî8ÎZHþ[ˆn88(>8)>8Þ8ŽjøÞŠj®8ÎŠˆ8>8þ8%ÆåÆîZHþ[ˆn88(>8)>8þz©>8îZIn8).Šh¾8þ8%Æâ Ð¢Ð¢6frÒö6öæf–r†÷2çF‚æ¦ö–â†BÂ'7FFR"’Ð¢6frç—VÆ–æRçöÆ—6‚ÒfÇ6PÐ¢6frç—VÆ–æRç&Wf–WrÒfÇ6PÐ¢6frç—VÆ–æRæ6öç6—7FVæ7•÷ÒfÇ6PÐ¢6frç—VÆ–æRæ&ööµ÷VæFW'7FæF–ærÒfÇ6PÐ¢6frç6VvÖVçBæÖ…ö6†'5÷W%ö&F6‚Ò Ð Ð¢6Æ–VçBÒf¶T6Æ–VçB††æFÆW#Ö†æFÆW"Ð¢÷&6†W7G&F÷"†6frÂ6Æ–VçCÖ6Æ–VçB’ç'Vâ‡G‡BÐ Ð¢G&ç6ÆFU÷&ö×G2Ò°Ð¢%Æâ"æ¦ö–â†Õ²&6öçFVçB%Òf÷"Ò–â5²&ÖW76vW2%ÒÐ¢f÷"2–â6Æ–VçBæ6ÆÇ0Ð¢–b.ih~ZÚn{û¾Šù"–â5²&ÖW76vW2%Õ³Õ²&6öçFVçB%ÐÐ¢ÐÐ¢6VÆbæ76W'Dw&VFW$WVÂ†ÆVâ‡G&ç6ÆFU÷&ö×G2’Â2Ð¢6VÆbæ76W'D–â‚.ZHþ[ˆn88(>8)2(i"[þZHþ[ˆb"ÂG&ç6ÆFU÷&ö×G5²ÓÒÐ Ð¢FVbFW7E÷&W7VÖU÷&V6÷fW'5ö&F6…övÆ÷76'•ö6†V6·ö–çG5ög&öÕöWfVçG2‡6VÆb“ Ð¢"".iz~x«nh{ºÞ‹yi{nZHÞyJŽh«ÞXùnK¨¾K»nûÈÎKˆÞK‹®[{.ZèÎh‰h›žjÊ˜xÞZHÞ‹>yJŽjŠYè¾8""" Ð¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2C Ð¢G‡BÒ÷2çF‚æ¦ö–â†BÂ&æ÷fVÂçG‡B"Ð¢w&—FU÷6×ÆU÷G‡B‡G‡BÐ¢6frÒö6öæf–r†÷2çF‚æ¦ö–â†BÂ'7FFR"’Ð¢6frç—VÆ–æRçöÆ—6‚ÒfÇ6PÐ¢6frç—VÆ–æRç&Wf–WrÒfÇ6PÐ¢6frç—VÆ–æRæ6öç6—7FVæ7•÷ÒfÇ6PÐ¢6frç—VÆ–æRæ&ööµ÷VæFW'7FæF–ærÒfÇ6PÐ¢6frç6VvÖVçBæÖ…ö6†'5÷W%ö&F6‚Ò€Ð Ð¢7F÷&RÒ÷&6†W7G&F÷"†6frÂ6Æ–VçCÔf¶T6Æ–VçB††æFÆW#×&÷WF–æuö†æFÆW"’’ç'Vâ€Ð¢G‡BÂöæÇ•ö6†FW#Ó Ð¢Ð¢6†V6·ö–çG2Ò7F÷&Ræ6ö×ÆWFVEö&F6…övÆ÷76'•ö¶W—2ƒÐ¢6VÆbæ76W'Dw&VFW"†ÆVâ†6†V6·ö–çG2’ÂÐ Ð¢2zº[{.ZèÎh‰KØnx«nhŠ*¾h.ZHÞK‹¢VæF–æ~ûÉ®{ºÞ‹y[©NK¸îK¨¾K»niz^[ù~ŠønXŠ¾[{.h«ÞXùnh›žjÊ8 Ð¢7F÷&Rç6WEö6†FW%÷7FGW2ƒÂ5DEU5õTäD”ärÐ Ð¢Æ&VÇ3¢Æ—7E·7G%ÒÒµÐÐ¢vÆ÷76'•öÆ&VÇ3¢Æ—7E·7G%ÒÒµÐÐ Ð¢FVb†æFÆW"†ÖW76vW2ÂF–W"Â§6öåöÖöFR“ Ð¢7—7FVÒÒÖW76vW5³Õ²&6öçFVçB%ÐÐ¢–b.iÊþŠúÒ"–â7—7FVÒæB.h«ÞXùnYš‚"–â7—7FVÓ Ð¢vÆ÷76'•öÆ&VÇ2æVæB†Æ&VÇ5²ÓÒÐ¢&WGW&â&÷WF–æuö†æFÆW"†ÖW76vW2ÂF–W"Â§6öåöÖöFRÐ Ð¢6Æ–VçBÒf¶T6Æ–VçB††æFÆW#Ö†æFÆW"Ð¢÷&6†W7G&F÷"†6frÂ6Æ–VçCÖ6Æ–VçB’ç'Vâ€Ð¢G‡BÀÐ¢öæÇ•ö6†FW#ÓÀÐ¢&öw&W73ÖÆÖ&FöFöæRÂ÷F÷FÂÂÆ&VÃ¢Æ&VÇ2æVæB†Æ&VÂ’ÀÐ¢Ð Ð¢vÆ÷76'•ö6ÆÇ2Ò°Ð¢6ÆÀÐ¢f÷"6ÆÂ–â6Æ–VçBæ6ÆÇ0Ð¢–b.iÊþŠúÒ"–â6ÆÅ²&ÖW76vW2%Õ³Õ²&6öçFVçB%ÐÐ¢æB.h«ÞXùnYš‚"–â6ÆÅ²&ÖW76vW2%Õ³Õ²&6öçFVçB%ÐÐ¢ÐÐ¢2[{.Šùh›žjÊXZŽ˜:Ž‹{>‹ø~ûÈÎXú®KùÞyYžzºiÊ¾KˆjÊXYÎ[©^h«ÞXùn8 Ð¢6VÆbæ76W'DWVÂ†ÆVâ†vÆ÷76'•ö6ÆÇ2’ÂÐ¢6VÆbæ76W'EG'VR†vÆ÷76'•öÆ&VÇ2Ð¢6VÆbæ76W'EG'VR†ÆÂ†Æ&VÂÒ.Šz>iéih~j>(
b"f÷"Æ&VÂ–âvÆ÷76'•öÆ&VÇ2’Ð Ð¢FVbFW7Eöf–æÅövÆ÷76'•ö—5öf–Æ&ÆU÷Fõ÷&Wf–Wu÷&ö×B‡6VÆb“ Ð¢"".Yîzºh˜Þh«ÞX{®y¨NiÊþŠúÞûÈÎK™þˆ;ÞyJŽK¨îK¸îzÊÎKˆzº[ÈZx¾y¨NiÈ{¸ŽZêj
8""" Ð Ð¢FVb†æFÆW"†ÖW76vW2ÂF–W"Â§6öåöÖöFR“ Ð¢7—7FVÒÒÖW76vW5³Õ²&6öçFVçB%ÐÐ¢W6W"ÒÖW76vW5²ÓÕ²&6öçFVçB%ÐÐ¢–b.ih~ZÚn{û¾Šù"–â7—7FVÓ Ð¢âÒÆVâ‡&Ræf–æFÆÂ‡"%åÅ²…ÆB²•ÅÒ"ÂW6W"Â&RäÕTÅD”Ä”äR’Ð¢&WGW&â§6öâæGV×2€Ð¢²'G&ç6ÆF–öç2#¢².[þZHþ[ˆb"f÷"ò–â&ævR†â•×ÒÂVç7W&Uö66–“ÔfÇ6PÐ¢Ð¢–b.iÊþŠúÒ"–â7—7FVÒæB.h«ÞXùnYš‚"–â7—7FVÒæB.[èÎXØ®8r"–âW6W# Ð¢&WGW&â§6öâæGV×2€Ð¢°Ð¢'FW&×2#¢°Ð¢°Ð¢'6÷W&6R#¢.ZHþ[ˆn88(>8)2"ÀÐ¢'F&vWB#¢.[þZHþ[ˆb"ÀÐ¢'G—R#¢.z{‹	2"ÀÐ¢&Æ–6W2#¢².ZHþ[ˆb%ÒÀÐ¢&æ÷FR#¢.K«.i‹^z{YÂ"ÀÐ¢ÐÐ¢ÐÐ¢ÒÀÐ¢Vç7W&Uö66–“ÔfÇ6RÀÐ¢Ð¢–b.iÊþŠúÒ"–â7—7FVÒæB.h«ÞXùnYš‚"–â7—7FVÓ Ð¢&WGW&â§6öâæGV×2‡²'FW&×2#¢µ×ÒÂVç7W&Uö66–“ÔfÇ6RÐ¢–b.iÊþŠúÞKˆˆ{Nh
~j
XxnYš‚"–â7—7FVÓ Ð¢6VÆbæ76W'D–â‚.8ÎZHþ[ˆn88(>8)>8Þ8ŽjøÞŠj®8ÎŠˆ8>8þ8""ÂW6W"Ð¢6VÆbæ76W'D–â‚r'F&vWB#¢.[þZHþ[ˆb"rÂW6W"Ð¢&WGW&â§6öâæGV×2€Ð¢²'FW&×2#¢·²'6÷W&6R#¢.ZHþ[ˆn88(>8)2"Â'F&vWB#¢.[þZHþ[ˆb'Õ×ÒÀÐ¢Vç7W&Uö66–“ÔfÇ6RÀÐ¢Ð¢–b.Šùih~Zêj
"–â7—7FVÓ Ð¢6VÆbæ76W'D–â‚.ZHþ[ˆn88(>8)2(i"[þZHþ[ˆb"ÂW6W"Ð¢&WGW&â÷&Wf–Wuö§6öâ‡W6W"ÂµÒÐ¢&WGW&â&÷WF–æuö†æFÆW"†ÖW76vW2ÂF–W"Â§6öåöÖöFRÐ Ð¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2C Ð¢G‡BÒ÷2çF‚æ¦ö–â†BÂ&æ÷fVÂçG‡B"Ð¢v—F‚÷Vâ‡G‡BÂ'r"ÂVæ6öF–æsÒ'WFbÓ‚"’2c Ð¢bçw&—FR€Ð¢"2zÊÎKˆzºÆåÆî8ÎZHþ[ˆn88(>8)>8Þ8ŽjøÞŠj®8ÎŠˆ8>8þ8%ÆåÆâ Ð¢"2zÊÎK¨ÎzºÆåÆî[èÎXØ®8~ZHþ[ˆn88(>8)>8ÎXhÞ8>xûî8(Î8þ8%Æâ Ð¢Ð¢6frÒö6öæf–r†÷2çF‚æ¦ö–â†BÂ'7FFR"’Ð¢6frç—VÆ–æRçöÆ—6‚ÒfÇ6PÐ¢6frç—VÆ–æRæ6öç6—7FVæ7•÷ÒfÇ6PÐ¢6frç—VÆ–æRæ&ööµ÷VæFW'7FæF–ærÒfÇ6PÐ¢6frç6VvÖVçBæÖ…ö6†'5÷W%ö&F6‚Ò# Ð Ð¢÷&6‚Ò÷&6†W7G&F÷"†6frÂ6Æ–VçCÔf¶T6Æ–VçB††æFÆW#Ö†æFÆW"’Ð¢÷&6‚ç'Vâ‡G‡BÐ¢÷&6‚ç'Vå÷&Wf–Wr‡G‡BÐ Ð Ð¦6Æ72FW7EF–W%&÷WF–ær‡Væ—GFW7BåFW7D66R“ Ð¢FVbFW7E÷F6µ÷F–W'2‡6VÆb“ Ð¢"".iË®j+K»¾Xª‹[f7Bj>8XŠNijÞ{¾‹[6†V8{û¾Šù‹[7G&öæ~ûÉ¾j)~jh.[ŠbÖ…÷Fö¶Vç2Kˆ®™™8""" Ð¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2C Ð¢G‡BÒ÷2çF‚æ¦ö–â†BÂ&æ÷fVÂçG‡B"Ð¢w&—FU÷6×ÆU÷G‡B‡G‡BÐ¢6frÒö6öæf–r†÷2çF‚æ¦ö–â†BÂ'7FFR"’Ð¢6frç—VÆ–æRæ&6·G&ç6ÆFU÷6×ÆRÒã2[Ë®X‹nŠznXùY¹îŠùÐ Ð¢6Æ–VçBÒf¶T6Æ–VçB††æFÆW#×&÷WF–æuö†æFÆW"Ð¢÷&6‚Ò÷&6†W7G&F÷"†6frÂ6Æ–VçCÖ6Æ–VçBÐ¢÷&6‚ç'Vâ‡G‡BÐ¢÷&6‚ç'Vå÷&Wf–Wr‡G‡BÐ Ð¢W‡V7BÒ°Ð¢.zºˆ¨.j)~jh.Y‚#¢&f7B"ÀÐ¢.XZŽKšnjh.ŠxŽY‚#¢&f7B"ÀÐ¢.iÊþŠúÞKˆîz{YÎh«ÞXùnYš‚#¢&f7B"ÀÐ¢.Y¹îŠùŠùˆR#¢&f7B"ÀÐ¢.Šùih~Zêj
#¢&6†V"ÀÐ¢.KùÞyÉþ[ªb#¢&6†V"ÀÐ¢.ih~ZÚn{û¾Šù#¢'7G&öær"ÀÐ¢ÐÐ¢6VVâÒ6WB‚Ð¢f÷"2–â6Æ–VçBæ6ÆÇ3 Ð¢7—7FVÒÒ5²&ÖW76vW2%Õ³Õ²&6öçFVçB%ÐÐ¢f÷"Ö&¶W"ÂF–W"–âW‡V7Bæ—FV×2‚“ Ð¢–bÖ&¶W"–â7—7FVÓ Ð¢6VÆbæ76W'DWVÂ†5²'F–W"%ÒÂF–W"Âb'¶Ö&¶W'Ò[©N‹[·F–W'Òj2"Ð¢6VVâæFB†Ö&¶W"Ð¢–bÖ&¶W"ÓÒ.zºˆ¨.j)~jh.Y‚# Ð¢6VÆbæ76W'DWVÂ†5²&Ö…÷Fö¶Vç2%ÒÂcÐ¢–bÖ&¶W"ÓÒ.XZŽKšnjh.ŠxŽY‚# Ð¢6VÆbæ76W'DWVÂ†5²&Ö…÷Fö¶Vç2%ÒÂ#Ð¢6VÆbæ76W'DWVÂ‡6VVâÂ6WB†W‡V7B’Â.YN{¾‹>yJŽ˜;Þ[©NX{®xë"Ð Ð Ð¦6Æ72FW7DÆætæ÷&ÖÆ—¦R‡Væ—GFW7BåFW7D66R“ Ð¢FVbFW7Eöæ÷&ÖÆ—¦UöÆær‡6VÆb“ Ð¢6VÆbæ76W'DWVÂ…öæ÷&ÖÆ—¦UöÆær‚$¦æW6R"’Â&¦"Ð¢6VÆbæ76W'DWVÂ…öæ÷&ÖÆ—¦UöÆær‚.iz^ŠúÒ"’Â&¦"Ð¢6VÆbæ76W'DWVÂ…öæ÷&ÖÆ—¦UöÆær‚%%R"’Â''R"Ð¢6VÆbæ76W'DWVÂ…öæ÷&ÖÆ—¦UöÆær‚''W76–â"’Â''R"Ð¢6VÆbæ76W'DWVÂ…öæ÷&ÖÆ—¦UöÆær‚&g""’Â&g""Ð¢6VÆbæ76W'DWVÂ…öæ÷&ÖÆ—¦UöÆær‚'Væ¶æ÷vâ"’Â""Ð¢6VÆbæ76W'DWVÂ…öæ÷&ÖÆ—¦UöÆær‚""’Â""Ð Ð Ð¦6Æ72FW7E&öw&W74Æ&VÇ2‡Væ—GFW7BåFW7D66R“ Ð¢FVbFW7E÷&öw&W75öÆ&VÅ÷&VfW'5÷&VÅ÷F—FÆR‡6VÆb“ Ð¢6VÆbæ76W'DWVÂ„÷&6†W7G&F÷"åö6†FW%÷&öw&W75öÆ&VÂ‚.[É^Šˆ"Â’Â.[É^Šˆ"Ð¢6VÆbæ76W'DWVÂ„÷&6†W7G&F÷"åö6†FW%÷&öw&W75öÆ&VÂ‚.zÊÎKˆzº"Â’Â.zÊÎKˆzº"Ð¢6VÆbæ76W'DWVÂ„÷&6†W7G&F÷"åö6†FW%÷&öw&W75öÆ&VÂ‚""Â’Â.zºˆ¨"""Ð Ð¢FVbFW7Eö6öç6—7FVæ7•öÆ&VÅ÷&VfW'5÷&VÅ÷F—FÆR‡6VÆb“ Ð¢g&öÒG&ç5öæ÷fVÂævVçG2æ6öç6—7FVæ7’–×÷'B6öç6—7FVæ7”6†V6¶W Ð Ð¢6VÆbæ76W'DWVÂ„6öç6—7FVæ7”6†V6¶W"åö6†FW%öÆ&VÂ‚.zÊÎKˆzº"Â’Â.zÊÎKˆzº"Ð¢6VÆbæ76W'DWVÂ„6öç6—7FVæ7”6†V6¶W"åö6†FW%öÆ&VÂ‚""Â’Â.zºˆ¨"""Ð Ð¢FVbFW7E÷&öw&W75ö6÷fW'5÷&W&F–öåöæEö÷WGWE÷7FvW2‡6VÆb“ Ð¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2C Ð¢G‡BÒ÷2çF‚æ¦ö–â†BÂ&æ÷fVÂçG‡B"Ð¢w&—FU÷6×ÆU÷G‡B‡G‡BÐ¢6frÒö6öæf–r†÷2çF‚æ¦ö–â†BÂ'7FFR"’Ð¢WfVçG3¢Æ—7E·GWÆU¶–çBÂ–çBÂ7G%ÕÒÒµÐÐ¢÷&6‚Ò÷&6†W7G&F÷"†6frÂ6Æ–VçCÔf¶T6Æ–VçB††æFÆW#×&÷WF–æuö†æFÆW"’Ð Ð¢÷&6‚ç'Vå÷7FW2€Ð¢G‡BÀÐ¢²'G&ç6ÆFR"Â'"Â'&W÷'B"Â&76VÖ&ÆR'ÒÀÐ¢&öw&W73ÖÆÖ&FFöæRÂF÷FÂÂÆ&VÃ¢WfVçG2æVæB‚†FöæRÂF÷FÂÂÆ&VÂ’’ÀÐ¢Ð Ð¢Æ&VÇ2Ò¶Æ&VÂf÷"òÂòÂÆ&VÂ–âWfVçG5ÐÐ¢W‡V7FVBÒ°Ð¢.Šz>iéih~j>(
b"ÀÐ¢.XˆniéXZŽKšnš8îjÎ(
b"ÀÐ¢.š(Nhš¾zºˆ¨.j)~jh""ÀÐ¢.yIþh‰XZŽKšnjh.ŠxŽ(
b"ÀÐ¢.{û¾Šùzºˆ¨.j~š)Ž(
b"ÀÐ¢.{û¾ŠùZèÎh‰"ÀÐ¢.Kˆˆ{Nh
r(
b"ÀÐ¢.yIþh‰hª^Y®(
b"ÀÐ¢.Y¹îZ¾Šùih~(
b"ÀÐ¢ÐÐ¢÷6—F–öç2Ò¶Æ&VÇ2æ–æFW‚†Æ&VÂ’f÷"Æ&VÂ–âW‡V7FVEÐÐ¢6VÆbæ76W'DWVÂ‡÷6—F–öç2Â6÷'FVB‡÷6—F–öç2’ÂÆ&VÇ2Ð¢6VÆbæ76W'D–â‚ƒÂÂ.yIþh‰XZŽKšnjh.ŠxŽ(
b"’ÂWfVçG2Ð Ð Ð¦–bõöæÖUõòÓÒ%õöÖ–åõò# Ð¢Væ—GFW7BæÖ–â‚Ð 