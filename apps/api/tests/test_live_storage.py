"""Durable publication and glossary edits while a whole-book worker is active."""

from __future__ import annotations

from unittest.mock import patch

import test_storage_pg_integration as storage_tests
from wenyi_api.storage_pg import PostgresStorage
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Segment
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.context import RollingContext
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translation_batch import BatchResult

pg_pool = storage_tests.pg_pool
pg_storage = storage_tests.pg_storage
initialize = storage_tests.initialize


def replacement(segment, target):
    return {
        "source": segment.source,
        "before": segment.target,
        "target": target,
        "expected_revision": PostgresStorage.segment_revision(segment),
    }


def test_stale_worker_save_retains_interactive_text_metadata_and_review_invalidation(
    pg_storage, tmp_path
):
    storage = pg_storage
    initialize(storage, tmp_path)
    chapter = storage.load_chapter(0)
    chapter.meta["review_passed"] = True
    chapter.segments[0].meta["docx_styles"] = {
        "items": [{"style": "italic"}],
        "target_digest": "old",
        "placements": [{"start": 0}],
    }
    chapter.segments.append(Segment(index=3, source="Other paragraph", target=None))
    storage.save_chapter(chapter)
    stale = storage.load_chapter(0)
    held_reference = stale.segments[0]
    publication = {0: replacement(held_reference, "用户选择重译的新文本")}
    assert storage.publish_retranslations(0, publication, request_id="request-1") == {
        "applied": [0],
        "conflicts": [],
    }
    # The whole-book worker finishes another batch plus an annotation alignment
    # based on its old chapter, and tries to save that stale chapter wholesale.
    stale.segments[0].target = "过期模型结果"
    stale.segments[0].meta["docx_styles"]["placements"] = [{"start": 999}]
    stale.segments[1].target = "另一段的新译文"
    storage.save_chapter_with_status(stale, "done")
    saved = storage.load_chapter(0)
    assert held_reference is stale.segments[0]
    assert held_reference.target == saved.segments[0].target == "用户选择重译的新文本"
    assert saved.segments[0].target_before_polish is None
    assert saved.segments[0].meta["docx_styles"] == {"items": [{"style": "italic"}]}
    assert saved.segments[1].target == "另一段的新译文"
    assert "review_passed" not in saved.meta
    assert saved.meta["review_invalidated_at"] == stale.meta["review_invalidated_at"]
    assert storage.load_manifest()["chapters"][0]["review_status"] == "pending"
    # A later retry after process restart sees the durable publication marker.
    restarted = PostgresStorage(storage.project_id, storage._pool, run_dir=storage.run_dir)
    assert restarted.publish_retranslations(0, publication, request_id="request-1") == {
        "applied": [0],
        "conflicts": [],
    }
    assert restarted.segment_revision(restarted.load_chapter(0).segments[0]) == 1


def test_publication_conflicts_preserve_newer_text_and_detect_aba(pg_storage, tmp_path):
    storage = pg_storage
    initialize(storage, tmp_path)
    before = storage.load_chapter(0).segments[0]
    pending = {0: replacement(before, "异步重译结果")}
    chapter = storage.load_chapter(0)
    chapter.segments[0].target = "先修改"
    storage.save_chapter(chapter)
    chapter.segments[0].target = before.target
    storage.save_chapter(chapter)
    assert storage.publish_retranslations(0, pending, request_id="stale-request") == {
        "applied": [],
        "conflicts": [0],
    }
    current = storage.load_chapter(0).segments[0]
    assert current.target == before.target
    assert storage.segment_revision(current) == 2
    assert "review_invalidated_at" not in storage.load_chapter(0).meta
    good = replacement(current, "接受新译文")
    bad = {**replacement(current, "不能覆盖"), "source": "different source"}
    assert storage.publish_retranslations(
        0, {0: good, 99: bad}, request_id="partly-applicable"
    ) == {"applied": [0], "conflicts": [99]}


def test_live_glossary_refreshes_after_checkpointed_resume_before_next_request(
    pg_storage, tmp_path
):
    storage = pg_storage
    initialize(storage, tmp_path)
    chapter = storage.load_chapter(0)
    chapter.segments[0].source = "Zed already translated"
    chapter.segments.append(Segment(index=1, source="Zed begins the next paragraph"))
    storage.save_chapter(chapter)
    storage.upsert_term(GlossaryTerm(source="Zed", target="旧名称"))
    storage.log_event("batch_glossary_extracted", chapter=0, start_index=0, count=1)
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"polish": False, "review": False, "book_understanding": False},
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    service = Orchestrator(config, client=FakeClient())._translation
    calls = []
    progress_calls = 0

    def progress(done, total, label):
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls == 2:  # After resuming a fully checkpointed batch.
            storage.edit_term("Zed", GlossaryTerm(source="Zed", target="编辑后名称"))

    def execute(plan, *, polish):
        calls.append([term.target for term in plan.terms])
        return BatchResult(("编辑后名称开始下一段",), (None,))

    with (
        patch.object(service._batches, "execute", side_effect=execute),
        patch.object(service._runtime.extractor, "extract_and_store", return_value={}),
    ):
        service.translate_chapter(
            0,
            storage,
            storage,
            RollingContext(),
            "",
            translation_history={},
            source_corpus="Zed Zed",
            annotation_context_registry=None,
            progress=progress,
            total=2,
        )
    assert calls == [["编辑后名称"]]
    assert storage.load_chapter(0).segments[1].target == "编辑后名称开始下一段"


def test_continuation_publication_invalidates_first_slice_layout(pg_storage, tmp_path):
    storage = pg_storage
    initialize(storage, tmp_path)
    chapter = storage.load_chapter(0)
    chapter.segments[0].meta["epub_annotations"] = {
        "items": [{"href": "#note"}],
        "placements": [{"start": 3}],
        "target_digest": "old",
    }
    chapter.segments.append(Segment(index=1, source="Continuation", target="旧续段", cont=True))
    storage.save_chapter(chapter)
    stale = storage.load_chapter(0)
    assert storage.publish_retranslations(
        0, {1: replacement(stale.segments[1], "新续段")}, request_id="continuation-edit"
    )["applied"] == [1]
    stale.segments[0].meta["epub_annotations"]["placements"] = [{"start": 999}]
    storage.save_chapter(stale)
    current = storage.load_chapter(0)
    assert current.segments[1].target == "新续段"
    assert current.segments[0].meta["epub_annotations"] == {"items": [{"href": "#note"}]}
    assert storage.segment_revision(current.segments[0]) == 1
