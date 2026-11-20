"""Background polishing with ``pipeline.polish_concurrency`` above 1."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest
from wenyi_core.config import PipelineConfig
from wenyi_core.llm.limits import RequestCancelled
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.translation import POLISH_PENDING
from wenyi_core.storage.run_store import STATUS_DONE, STATUS_PENDING

from tests.fake_llm import MeteredFakeClient, routing_handler
from tests.pipeline_fixtures import fake_pipeline_config, require_file_storage
from tests.sample_data import write_sample_txt


class SlowPolishClient(MeteredFakeClient):
    """Delay polish calls and record how many run at once and what overlaps them."""

    def __init__(self, *, delay: float = 0.05, polish_hook=None):
        super().__init__(handler=routing_handler)
        self.delay = delay
        self.polish_hook = polish_hook
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.translations_during_polish = 0

    def complete(self, messages, *, operation, **kwargs):
        if operation != "polish.body":
            if operation == "translation.body":
                with self._lock:
                    self.translations_during_polish += bool(self.active)
            return super().complete(messages, operation=operation, **kwargs)
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            if self.polish_hook is not None:
                self.polish_hook(messages)
            return super().complete(messages, operation=operation, **kwargs)
        finally:
            with self._lock:
                self.active -= 1


def _setup(tmp_path, workers: int):
    txt = os.path.join(tmp_path, "novel.txt")
    write_sample_txt(txt)
    config = fake_pipeline_config(os.path.join(tmp_path, "state"))
    config.pipeline.review = False
    config.pipeline.book_understanding = False
    config.pipeline.polish_concurrency = workers
    # One paragraph per batch gives seven polish jobs (headings included) in two chapters.
    config.segment.max_tokens_per_batch = 1
    return txt, config


def _events(store, name: str) -> list[dict]:
    with open(store.event_log_path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [row for row in rows if row.get("event") == name]


def _segments(store):
    manifest = store.load_manifest()
    return [
        segment
        for row in manifest["chapters"]
        for segment in store.load_chapter(row["index"]).text_segments
    ]


def test_polish_concurrency_validates_lower_bound():
    with pytest.raises(ValueError):
        PipelineConfig(polish_concurrency=0)
    assert PipelineConfig().polish_concurrency == 1


def test_background_polish_overlaps_translation_within_bound(tmp_path):
    txt, config = _setup(tmp_path, workers=2)
    client = SlowPolishClient(delay=0.2)
    store = require_file_storage(Orchestrator(config, client=client).run(txt))

    assert client.peak == 2
    assert client.translations_during_polish > 0
    segments = _segments(store)
    assert len(segments) == 7
    for segment in segments:
        assert segment.target is not None and segment.target.startswith("润")
        assert segment.target_before_polish and not segment.target_before_polish.startswith("润")
        assert POLISH_PENDING not in segment.meta
    assert all(row["status"] == STATUS_DONE for row in store.load_manifest()["chapters"])
    assert len(_events(store, "batch_polish_queued")) == 7
    assert len(_events(store, "batch_polished")) == 7
    # Each chapter is published only after all of its polish jobs are applied.
    polished_chapters = [row["chapter"] for row in _events(store, "batch_polished")]
    done = [row["chapter"] for row in _events(store, "chapter_done")]
    assert done == [0, 1]
    assert polished_chapters == sorted(polished_chapters)
    usage = store.load_usage()
    assert usage is not None
    assert usage["totals"] == client.usage_summary()["totals"]
    polish_calls = sum(1 for call in client.calls if call["operation"] == "polish.body")
    assert usage["by_stage"]["polish.body"]["calls"] == polish_calls


def test_interrupted_polish_resumes_without_retranslation(tmp_path):
    txt, config = _setup(tmp_path, workers=3)

    def cancel(_messages):
        raise RequestCancelled("stop")

    client = SlowPolishClient(delay=0.01, polish_hook=cancel)
    orchestrator = Orchestrator(config, client=client)
    with pytest.raises(RequestCancelled):
        orchestrator.run(txt)
    store = require_file_storage(orchestrator.prepare(txt))
    drafted = [segment for segment in _segments(store) if segment.target is not None]
    assert drafted, "at least one draft must be saved before the interruption"
    assert all(segment.meta.get(POLISH_PENDING) for segment in drafted)
    assert all(segment.target == segment.target_before_polish for segment in drafted)

    client.polish_hook = None
    first_run_calls = len(client.calls)
    orchestrator.run(txt)
    resumed = client.calls[first_run_calls:]
    translated = [call for call in resumed if call["operation"] == "translation.body"]
    polished = [call for call in resumed if call["operation"] == "polish.body"]
    # Saved drafts are repolished in a fresh conversation; only missing ones are translated.
    assert len(translated) == 7 - len(drafted)
    assert len(polished) == 7
    for segment in _segments(store):
        assert segment.target is not None and segment.target.startswith("润")
        assert POLISH_PENDING not in segment.meta


@pytest.mark.parametrize("failure", ["misaligned", "error"])
def test_failed_polish_keeps_draft_and_continues(tmp_path, failure):
    txt, config = _setup(tmp_path, workers=2)
    client = SlowPolishClient(delay=0.01)
    orchestrator = Orchestrator(config, client=client)
    executor = orchestrator._translation._batches
    polish = executor.polish

    def fail_one(job):
        if any("屋上" in source for source in job.sources):
            if failure == "error":
                raise RuntimeError("polisher unavailable")
            return None
        return polish(job)

    executor.polish = fail_one
    store = require_file_storage(orchestrator.run(txt))

    failed = _events(store, "batch_polish_failed")
    assert len(failed) == 1
    assert failed[0]["reason"] == (
        "misaligned_output" if failure == "misaligned" else "RuntimeError: polisher unavailable"
    )
    segments = _segments(store)
    drafts = [segment for segment in segments if not segment.target.startswith("润")]
    assert [segment.source for segment in drafts] == [
        segment.source for segment in segments if "屋上" in segment.source
    ]
    assert drafts[0].target == drafts[0].target_before_polish
    assert all(POLISH_PENDING not in segment.meta for segment in segments)
    assert all(row["status"] == STATUS_DONE for row in store.load_manifest()["chapters"])


def test_interruption_waits_for_running_polish_and_records_its_usage(tmp_path):
    txt, config = _setup(tmp_path, workers=2)
    calls = {"n": 0}
    lock = threading.Lock()

    def first_cancels_second_is_slow(_messages):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n == 1:
            raise RequestCancelled("stop")
        if n == 2:
            time.sleep(0.5)

    client = SlowPolishClient(delay=0.01, polish_hook=first_cancels_second_is_slow)
    orchestrator = Orchestrator(config, client=client)
    with pytest.raises(RequestCancelled):
        orchestrator.run(txt)
    assert not [t for t in threading.enumerate() if t.name.startswith("wenyi-polish")]
    assert client.active == 0
    store = require_file_storage(orchestrator.prepare(txt))
    usage = store.load_usage()
    assert usage is not None
    assert usage["totals"] == client.usage_summary()["totals"]


@pytest.mark.parametrize("workers", [1, 3])
def test_background_polish_context_reads_drafts_regardless_of_timing(tmp_path, workers):
    txt, config = _setup(tmp_path, workers=workers)
    # A fast polisher would finish before the next batch if context depended on timing.
    client = SlowPolishClient(delay=0)
    Orchestrator(config, client=client).run(txt)
    prompts = [
        call["messages"][-1]["content"]
        for call in client.calls
        if call["operation"] == "translation.body"
    ]
    assert len(prompts) == 7
    # Inline polishing feeds polished text forward; background polishing always uses drafts.
    assert any("润" in prompt for prompt in prompts) is (workers == 1)


def test_resume_repolishes_only_marked_segments_of_a_mixed_batch(tmp_path):
    txt, config = _setup(tmp_path, workers=2)
    config.pipeline.polish = False
    orchestrator = Orchestrator(config, client=SlowPolishClient(delay=0))
    store = require_file_storage(orchestrator.run(txt))
    chapter = store.load_chapter(0)
    segments = chapter.text_segments
    for segment in segments:
        # Final drafts, for example kept after a failed polish.
        segment.target_before_polish = segment.target
    segments[1].meta[POLISH_PENDING] = True
    store.save_chapter(chapter)
    store.set_chapter_status(0, STATUS_PENDING)
    drafts = [segment.target for segment in segments]

    # A larger budget merges the pending draft with final drafts into one resume batch.
    config.pipeline.polish = True
    config.segment.max_tokens_per_batch = 1800
    orchestrator.run(txt)

    resumed = store.load_chapter(0).text_segments
    assert resumed[1].target is not None and resumed[1].target.startswith("润")
    assert resumed[1].target_before_polish == drafts[1]
    assert [s.target for i, s in enumerate(resumed) if i != 1] == [
        d for i, d in enumerate(drafts) if i != 1
    ]
    assert all(POLISH_PENDING not in segment.meta for segment in resumed)
