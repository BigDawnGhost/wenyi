"""Regressions for polish empty-segment guards, None-only export fallback, and polish alignment retry."""

from __future__ import annotations

import json
import os
import re
import zipfile

import pytest
from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from wenyi_core.agents.polisher import Polisher
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.translation import POLISH_FAILED
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import fake_pipeline_config, require_file_storage
from tests.sample_data import write_sample_epub, write_sample_txt


def _events(store: FileStorage, name: str) -> list[dict]:
    return [row for row in store.list_events() if row.get("event") == name]


@pytest.mark.parametrize("via_standalone", [False, True], ids=["continue", "standalone"])
def test_polish_blanking_nonempty_segment_keeps_draft_and_logs_warning(tmp_path, via_standalone):
    txt = os.path.join(tmp_path, "novel.txt")
    write_sample_txt(txt)
    config = fake_pipeline_config(os.path.join(tmp_path, "state"))
    config.pipeline.review = False
    config.pipeline.book_understanding = False
    config.pipeline.polish = True

    def handler(messages, tier, json_mode):
        last = messages[-1]["content"]
        if len(messages) == 4:
            if via_standalone:
                return json.dumps({"polished": []}, ensure_ascii=False)
            prev = json.loads(messages[2]["content"])
            n = len(prev.get("translations", []))
            out = [f"润{i}" for i in range(n)]
            if n:
                out[0] = ""
            return json.dumps({"polished": out}, ensure_ascii=False)
        if "editor" in messages[0]["content"].lower():
            count = len(re.findall(r"^\[\d+\] ", last, re.MULTILINE))
            if count:
                out = [f"润{i}" for i in range(count)]
                out[0] = ""
                return json.dumps({"polished": out}, ensure_ascii=False)
        return routing_handler(messages, tier, json_mode)

    store = require_file_storage(Orchestrator(config, client=FakeClient(handler=handler)).run(txt))
    kept_events = _events(store, "batch_polish_empty_kept")
    assert kept_events, "expected a warning event when polish blanked a non-empty draft"
    assert 0 in kept_events[0]["reverted_indices"]
    # The blanked first segment kept its non-empty translation draft instead of becoming "".
    first = store.load_chapter(0).text_segments[0]
    assert first.target and first.target.strip()
    assert first.target == first.target_before_polish


def test_polisher_retries_misalignment_and_splits_before_giving_up():
    config = Config.from_dict({"llm": {"preset": "fake"}, "pipeline": {"align_retry_limit": 1}})

    # Case 1: misaligns once, then aligns on retry.
    calls = {"n": 0}

    def retry_handler(messages, tier, json_mode):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"polished": ["only-one"]})
        return json.dumps({"polished": ["A!", "B!"]})

    polisher = Polisher(FakeClient(handler=retry_handler), config)
    assert polisher.polish_or_none(["A", "B"]) == ["A!", "B!"]
    assert calls["n"] == 2

    # Case 2: full 4-item batch always misaligns, but halves (2+2) align cleanly.
    split_calls = []

    def split_handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        items = re.findall(r"^\[\d+\] (.*)$", user, re.MULTILINE)
        split_calls.append(len(items))
        if len(items) > 2:
            return json.dumps({"polished": ["wrong-count"]})
        return json.dumps({"polished": [f"P:{text}" for text in items]})

    polisher_split = Polisher(FakeClient(handler=split_handler), config)
    assert polisher_split.polish_or_none(["a", "b", "c", "d"]) == ["P:a", "P:b", "P:c", "P:d"]
    assert split_calls == [4, 4, 2, 2]

    # Case 3: always misaligns -> returns None cleanly without O(N) explosion.
    always_calls = []

    def always_bad(messages, tier, json_mode):
        user = messages[-1]["content"]
        items = re.findall(r"^\[\d+\] (.*)$", user, re.MULTILINE)
        always_calls.append(len(items))
        return json.dumps({"polished": []})

    polisher_bad = Polisher(FakeClient(handler=always_bad), config)
    assert polisher_bad.polish_or_none(["a", "b", "c", "d"]) is None
    # Tries 4 (x2), then left half 2 (x2), then left leaf 1 (x2), and short-circuits.
    assert always_calls == [4, 4, 2, 2, 1, 1]

    # Case 4: non-resumable provider error fails fast after 1 call without retrying or splitting.
    err_calls = {"n": 0}

    def fail_runtime(*_args):
        err_calls["n"] += 1
        raise RuntimeError("503 Service Unavailable")

    polisher_err = Polisher(FakeClient(handler=fail_runtime), config)
    assert polisher_err.polish_or_none([f"p{i}" for i in range(16)]) is None
    assert err_calls["n"] == 1

    # Case 5: resumable provider outage propagates after 1 call so runner pauses & auto-resumes.
    outage_calls = {"n": 0}

    def fail_outage(*_args):
        outage_calls["n"] += 1
        raise TimeoutError("provider timed out")

    polisher_outage = Polisher(FakeClient(handler=fail_outage), config)
    with pytest.raises(TimeoutError, match="provider timed out"):
        polisher_outage.polish_or_none([f"p{i}" for i in range(16)])
    assert outage_calls["n"] == 1

    # Case 6: polish_continue also propagates resumable provider outage immediately.
    cont_calls = {"n": 0}

    def fail_cont(*_args):
        cont_calls["n"] += 1
        raise TimeoutError("continue timed out")

    turn = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
        {"role": "assistant", "content": json.dumps({"translations": ["a", "b"]})},
    ]
    polisher_cont = Polisher(FakeClient(handler=fail_cont), config)
    with pytest.raises(TimeoutError, match="continue timed out"):
        polisher_cont.polish_continue(turn, n=2)
    assert cont_calls["n"] == 1


def test_unpolished_chapter_is_marked_and_repolished_on_resume_without_retranslating(tmp_path):
    txt = os.path.join(tmp_path, "novel.txt")
    write_sample_txt(txt)
    config = fake_pipeline_config(os.path.join(tmp_path, "state"))
    config.pipeline.review = False
    config.pipeline.book_understanding = False
    config.pipeline.polish = True
    config.pipeline.align_retry_limit = 0

    allow_polish = {"ok": False}

    def handler(messages, tier, json_mode):
        if len(messages) == 4 or "editor" in messages[0]["content"].lower():
            if not allow_polish["ok"]:
                return json.dumps({"polished": []})
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=handler)
    store = require_file_storage(Orchestrator(config, client=client).run(txt))
    manifest = store.load_manifest()
    assert all(row.get("unpolished") is True for row in manifest["chapters"])
    assert store.load_chapter(0).meta.get("unpolished") is True
    assert any(seg.meta.get(POLISH_FAILED) for seg in store.load_chapter(0).text_segments)
    finished = _events(store, "translate_run_finished")[-1]
    assert finished["unpolished_chapter_count"] == len(manifest["chapters"])
    assert finished["unpolished_chapters"] == [row["index"] for row in manifest["chapters"]]
    draft_before = store.load_chapter(0).text_segments[0].target
    assert draft_before and not draft_before.startswith("润")

    # Manually edit one segment in chapter 0 before resuming; resume must preserve the manual edit.
    ch0_edited = store.load_chapter(0)
    if len(ch0_edited.text_segments) > 1:
        ch0_edited.text_segments[1].target = "人工修改译文"
        store.save_chapter(ch0_edited)

    # Resume with polish working again: re-polishes without issuing any translation.body calls!
    allow_polish["ok"] = True
    resume_client = FakeClient(handler=handler)
    Orchestrator(config, client=resume_client).run(txt)
    assert not [c for c in resume_client.calls if c["operation"] == "translation.body"]
    assert any(c["operation"] == "polish.body" for c in resume_client.calls)
    manifest_after = store.load_manifest()
    assert not any(row.get("unpolished") for row in manifest_after["chapters"])
    assert not store.load_chapter(0).meta.get("unpolished")
    ch0_after = store.load_chapter(0)
    assert all(POLISH_FAILED not in seg.meta for seg in ch0_after.text_segments)
    assert ch0_after.text_segments[0].target.startswith("润")
    if len(ch0_after.text_segments) > 1:
        assert ch0_after.text_segments[1].target == "人工修改译文"
    finished_after = _events(store, "translate_run_finished")[-1]
    assert finished_after["unpolished_chapter_count"] == 0


def test_unpolished_flag_cleared_when_resumed_with_polish_disabled(tmp_path):
    txt = os.path.join(tmp_path, "novel.txt")
    write_sample_txt(txt)
    config = fake_pipeline_config(os.path.join(tmp_path, "state"))
    config.pipeline.review = False
    config.pipeline.book_understanding = False
    config.pipeline.polish = True
    config.pipeline.align_retry_limit = 0

    def fail_polish(messages, tier, json_mode):
        if len(messages) == 4 or "editor" in messages[0]["content"].lower():
            return json.dumps({"polished": []})
        return routing_handler(messages, tier, json_mode)

    store = require_file_storage(
        Orchestrator(config, client=FakeClient(handler=fail_polish)).run(txt)
    )
    assert store.load_chapter(0).meta.get("unpolished") is True
    assert store.load_manifest()["chapters"][0].get("unpolished") is True

    config.pipeline.polish = False
    client_off = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client_off).run(txt, only_chapter=0)
    assert not [
        c for c in client_off.calls if c["operation"] in {"translation.body", "polish.body"}
    ]
    assert not store.load_chapter(0).meta.get("unpolished")
    assert not store.load_manifest()["chapters"][0].get("unpolished")
    assert all(POLISH_FAILED not in seg.meta for seg in store.load_chapter(0).text_segments)


def test_polish_provider_outage_saves_draft_and_resumes_without_retranslating(tmp_path):
    txt = os.path.join(tmp_path, "novel.txt")
    write_sample_txt(txt)
    config = fake_pipeline_config(os.path.join(tmp_path, "state"))
    config.pipeline.review = False
    config.pipeline.book_understanding = False
    config.pipeline.polish = True
    config.pipeline.align_retry_limit = 0

    allow_polish = {"ok": False}

    def handler(messages, tier, json_mode):
        if len(messages) == 4 or "editor" in messages[0]["content"].lower():
            if not allow_polish["ok"]:
                raise TimeoutError("polisher timed out")
        return routing_handler(messages, tier, json_mode)

    with pytest.raises(TimeoutError, match="polisher timed out"):
        Orchestrator(config, client=FakeClient(handler=handler)).run(txt, only_chapter=0)

    store = require_file_storage(
        Orchestrator(config, client=FakeClient(handler=routing_handler)).prepare(txt)
    )
    ch0_saved = store.load_chapter(0)
    assert all(seg.target is not None for seg in ch0_saved.text_segments)
    assert any(seg.meta.get(POLISH_FAILED) for seg in ch0_saved.text_segments)
    failed_events = _events(store, "batch_polish_failed")
    assert any(ev.get("reason") == "provider_interrupted" for ev in failed_events)

    allow_polish["ok"] = True
    resume_client = FakeClient(handler=handler)
    Orchestrator(config, client=resume_client).run(txt, only_chapter=0)
    assert not [c for c in resume_client.calls if c["operation"] == "translation.body"]
    assert any(c["operation"] == "polish.body" for c in resume_client.calls)
    ch0_after = store.load_chapter(0)
    assert all(POLISH_FAILED not in seg.meta for seg in ch0_after.text_segments)
    assert ch0_after.text_segments[0].target.startswith("润")


@pytest.mark.parametrize("bilingual", [False, True], ids=["mono", "bilingual"])
def test_export_only_falls_back_on_none_and_keeps_empty_target_blank(tmp_path, bilingual):
    # 1. Generated EPUB / DOCX / HTML / TXT / Markdown from text document.
    src_txt = tmp_path / "book.txt"
    src_txt.write_text("# Heading\n\nPendingSrc\n\nEmptySrc\n\nNormalSrc\n", encoding="utf-8")
    store = FileStorage(str(tmp_path / "state_txt"))
    store.init_from_document(
        Document(
            title="Book",
            source_lang="en",
            target_lang="zh",
            fmt="txt",
            source_path=str(src_txt),
            chapters=[
                Chapter(
                    index=0,
                    title="Heading",
                    segments=[
                        Segment(index=0, source="PendingSrc", target=None),
                        Segment(index=1, source="EmptySrc", target=""),
                        Segment(index=2, source="NormalSrc", target="正常译文"),
                    ],
                )
            ],
        )
    )

    # HTML
    html_out = tmp_path / f"out_{bilingual}.html"
    assemble(store, str(src_txt), out_path=str(html_out), out_format="html", bilingual=bilingual)
    html_text = html_out.read_text(encoding="utf-8")
    assert "PendingSrc" in html_text
    assert "EmptySrc" not in html_text
    assert "正常译文" in html_text
    assert ("NormalSrc" in html_text) is bilingual

    # TXT
    txt_out = tmp_path / f"out_{bilingual}.txt"
    assemble(store, str(src_txt), out_path=str(txt_out), out_format="txt", bilingual=bilingual)
    txt_text = txt_out.read_text(encoding="utf-8")
    assert "PendingSrc" in txt_text
    assert "EmptySrc" not in txt_text
    assert "正常译文" in txt_text
    assert ("NormalSrc" in txt_text) is bilingual

    # Markdown
    md_out = tmp_path / f"out_{bilingual}.md"
    assemble(store, str(src_txt), out_path=str(md_out), out_format="markdown", bilingual=bilingual)
    md_text = md_out.read_text(encoding="utf-8")
    assert "PendingSrc" in md_text
    assert "EmptySrc" not in md_text
    assert "正常译文" in md_text
    assert ("NormalSrc" in md_text) is bilingual

    # DOCX
    docx_out = tmp_path / f"out_{bilingual}.docx"
    assemble(store, str(src_txt), out_path=str(docx_out), out_format="docx", bilingual=bilingual)
    docx_paras = [p.text for p in DocxDocument(str(docx_out)).paragraphs]
    joined_docx = "\n".join(docx_paras)
    assert "PendingSrc" in joined_docx
    assert "EmptySrc" not in joined_docx
    assert "正常译文" in joined_docx
    assert ("NormalSrc" in joined_docx) is bilingual

    # Generated EPUB
    epub_out = tmp_path / f"out_{bilingual}.epub"
    assemble(store, str(src_txt), out_path=str(epub_out), out_format="epub", bilingual=bilingual)
    with zipfile.ZipFile(epub_out) as zf:
        ch_name = next(name for name in zf.namelist() if name.endswith("ch0.xhtml"))
        ch_html = zf.read(ch_name).decode("utf-8")
    assert "PendingSrc" in ch_html
    assert "EmptySrc" not in ch_html
    assert "正常译文" in ch_html
    assert ("NormalSrc" in ch_html) is bilingual

    # 2. Template-backfill EPUB path.
    src_epub = tmp_path / "sample.epub"
    write_sample_epub(str(src_epub))
    epub_cfg = fake_pipeline_config(str(tmp_path / "state_epub"))
    epub_cfg.pipeline.book_understanding = False
    epub_cfg.pipeline.polish = False
    epub_cfg.pipeline.review = False
    epub_store = require_file_storage(
        Orchestrator(epub_cfg, client=FakeClient(handler=routing_handler)).prepare(str(src_epub))
    )
    ch0 = epub_store.load_chapter(0)
    assert len(ch0.text_segments) >= 3
    pending_src = ch0.text_segments[0].source
    empty_src = ch0.text_segments[1].source
    normal_src = ch0.text_segments[2].source
    ch0.text_segments[0].target = None
    ch0.text_segments[1].target = ""
    ch0.text_segments[2].target = "模板译文"
    epub_store.save_chapter(ch0)
    tpl_out = tmp_path / f"tpl_{bilingual}.epub"
    assemble(
        epub_store, str(src_epub), out_path=str(tpl_out), out_format="epub", bilingual=bilingual
    )
    with zipfile.ZipFile(tpl_out) as zf:
        xhtml_names = [n for n in zf.namelist() if n.endswith(".xhtml") and "ch1" in n]
        assert xhtml_names
        soup = BeautifulSoup(zf.read(xhtml_names[0]).decode("utf-8"), "html.parser")
        body_text = soup.body.get_text() if soup.body else soup.get_text()
    assert pending_src in body_text
    assert empty_src not in body_text
    assert "模板译文" in body_text
    assert (normal_src in body_text) is bilingual
