"""PDF metadata uses the shared aligner without changing formal translations."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from wenyi_core.agents.annotation_aligner import target_digest
from wenyi_core.assemble.pdf_writer import _assemble_pdf
from wenyi_core.config import Config
from wenyi_core.ingest.pdf_babeldoc import _segment_from_para, annotations_from_store
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.pdf_bridge import BabeldocBridgeClient, BabeldocBridgeError
from wenyi_core.pipeline.annotate.annotations import AnnotationService
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage


def annotation():
    return {
        "id": "link:0",
        "kind": "link",
        "mode": "range",
        "source_start": 0,
        "source_end": 3,
        "link_id": "0:0",
    }


def test_pdf_ingest_retains_annotation_offsets_and_source_whitespace():
    source = " Ada met Bo. "
    item = {**annotation(), "source_start": 1, "source_end": 4}
    segment = _segment_from_para(3, {"id": "0:2", "source": source, "annotations": [item]})
    assert segment is not None
    assert segment.source == source
    assert segment.meta["pdf_annotations"] == {
        "source_length": len(source),
        "items": [item],
    }


@pytest.fixture(params=[FileStorage, SqliteStorage])
def project(tmp_path, request):
    segment = Segment(
        index=0,
        source="Ada met Bo.",
        target="阿达见到了博。",
        meta={
            "babeldoc_id": "0:0",
            "pdf_annotations": {"source_length": 11, "items": [annotation()]},
        },
    )
    store = request.param(str(tmp_path / "run"))
    manifest = store.stage_document(
        Document(
            title="Synthetic PDF",
            fmt="pdf",
            source_lang="en",
            target_lang="zh",
            chapters=[Chapter(index=0, segments=[segment])],
        ),
        source_hash="0" * 64,
    )
    store.save_manifest(manifest)
    try:
        yield store
    finally:
        store.close()


def service():
    client = FakeClient(
        handler=lambda *_: json.dumps(
            {"target_quote": "阿达", "context": "阿达见到了博"}, ensure_ascii=False
        )
    )
    config = Config.from_dict({"llm": {"preset": "fake"}})
    return AnnotationService(PipelineRuntime(config, client=client)), client


def test_pdf_uses_shared_alignment_cache_and_read_only_export(project):
    annotations, client = service()
    chapter = project.load_chapter(0)
    original = chapter.segments[0].target
    annotations.align_annotations_after_batch(0, chapter, 0, 1, project)
    metadata = project.load_chapter(0).segments[0].meta["pdf_annotations"]
    assert metadata["target_digest"] == target_digest(original)
    assert metadata["placements"][0]["target_end"] == 2
    assert len(client.calls) == 1
    assert client.calls[0]["stage"] == "annotation.align"
    annotations.align_annotations_after_batch(0, chapter, 0, 1, project)
    assert len(client.calls) == 1
    before = project.load_chapter(0).model_dump()
    payload = annotations_from_store(project, {"0:0": original})
    assert payload == {
        "0:0": {
            "target_digest": metadata["target_digest"],
            "placements": metadata["placements"],
        }
    }
    assert project.load_chapter(0).model_dump() == before
    assert chapter.segments[0].target == original


def test_pdf_export_preserves_text_with_stale_offsets_and_resume_refreshes_them(project):
    annotations, client = service()
    chapter = project.load_chapter(0)
    annotations.align_annotations_after_batch(0, chapter, 0, 1, project)
    chapter.segments[0].target = "今天阿达见到了博。"
    project.save_chapter(chapter)
    before = project.load_chapter(0).model_dump()
    payload = annotations_from_store(project, {"0:0": chapter.segments[0].target})
    assert payload["0:0"]["target_digest"] != target_digest(chapter.segments[0].target)
    assert project.load_chapter(0).model_dump() == before
    annotations.repair_fallbacks(project)
    result = annotations_from_store(project, {"0:0": chapter.segments[0].target})
    assert result["0:0"]["placements"][0]["target_start"] == 2
    assert len(client.calls) == 2


def test_untranslated_pdf_exports_identity_positions_without_model(project):
    text = project.load_chapter(0).segments[0].source
    result = annotations_from_store(project, {"0:0": text})
    placement = result["0:0"]["placements"][0]
    assert (placement["target_start"], placement["target_end"]) == (0, 3)
    assert placement["status"] == "aligned"


def test_pdf_formula_fallback_is_forwarded_without_replacing_translation(project):
    chapter = project.load_chapter(0)
    chapter.segments[0].meta["pdf_annotations"]["items"][0]["kind"] = "formula"
    project.save_chapter(chapter)
    annotations, _ = service()
    annotations._runtime.annotation_aligner.client = FakeClient(handler=lambda *_: "{}")
    annotations.align_annotations_after_batch(0, chapter, 0, 1, project)
    before = project.load_chapter(0).model_dump()
    payload = annotations_from_store(project, {"0:0": chapter.segments[0].target})
    assert payload["0:0"]["placements"][0]["status"] == "fallback"
    assert project.load_chapter(0).model_dump() == before
    calls = len(annotations._runtime.annotation_aligner.client.calls)
    annotations.repair_fallbacks(project)
    assert project.load_chapter(0).model_dump() == before
    assert len(annotations._runtime.annotation_aligner.client.calls) == calls


def test_pdf_assembly_keeps_valid_and_failed_items_in_the_same_translated_paragraph(
    project, monkeypatch, tmp_path
):
    annotations, client = service()
    chapter = project.load_chapter(0)
    annotations.align_annotations_after_batch(0, chapter, 0, 1, project)
    metadata = chapter.segments[0].meta["pdf_annotations"]
    metadata["items"].append({**annotation(), "id": "link:failed", "link_id": "0:1"})
    metadata["placements"].append(
        {
            "id": "link:failed",
            "mode": "range",
            "target_start": len(chapter.segments[0].target),
            "target_end": len(chapter.segments[0].target),
            "status": "fallback",
        }
    )
    project.save_chapter(chapter)
    manifest = project.load_manifest()
    manifest["meta"] = {
        "babeldoc_session_id": "session",
        "babeldoc_bridge_url": "http://bridge.invalid",
    }
    project.save_manifest(manifest)
    calls = []

    def fillback(self, session, text, *, out_path, annotations):
        calls.append((text, annotations))
        return str(out_path)

    monkeypatch.setattr(BabeldocBridgeClient, "fillback", fillback)
    before = project.load_chapter(0).model_dump()
    _assemble_pdf(project, "unused.pdf", str(tmp_path / "out.pdf"), timeout=30)
    assert calls[0][0]["0:0"] == "阿达见到了博。"
    assert [row["status"] for row in calls[0][1]["0:0"]["placements"]] == [
        "aligned",
        "fallback",
    ]
    assert project.load_chapter(0).model_dump() == before
    assert len(client.calls) == 1


def test_pdf_assembly_forwards_cached_alignment(project, monkeypatch, tmp_path):
    annotations, _ = service()
    chapter = project.load_chapter(0)
    annotations.align_annotations_after_batch(0, chapter, 0, 1, project)
    manifest = project.load_manifest()
    manifest["meta"] = {
        "babeldoc_session_id": "session",
        "babeldoc_bridge_url": "http://bridge.invalid",
    }
    project.save_manifest(manifest)
    calls = []

    def fillback(self, session, text, *, out_path, annotations):
        calls.append((text, annotations))
        return str(out_path)

    monkeypatch.setattr(BabeldocBridgeClient, "fillback", fillback)
    _assemble_pdf(project, "unused.pdf", str(tmp_path / "out.pdf"), timeout=30)
    assert calls[0][1] == annotations_from_store(project, calls[0][0])


def test_pdf_client_requires_bridge_capability_before_publishing(tmp_path, monkeypatch):
    client = BabeldocBridgeClient("http://bridge.invalid")
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous complete export")
    monkeypatch.setattr(client, "health", lambda: {"ok": True})
    with pytest.raises(BabeldocBridgeError, match="Update and restart"):
        client.fillback("session", {"0:0": "正文"}, out_path=output, annotations={"0:0": {}})
    assert output.read_bytes() == b"previous complete export"


def test_pdf_corner_mark_uses_existing_point_alignment_without_inserting_into_target(project):
    chapter = project.load_chapter(0)
    item = {
        "id": "formula:1",
        "kind": "formula",
        "mode": "point",
        "source_start": 10,
        "source_end": 10,
        "composition_index": 1,
        "link_ids": ["0:0"],
    }
    chapter.segments[0].meta["pdf_annotations"]["items"] = [item]
    project.save_chapter(chapter)

    def respond(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        assert payload["source_with_markers"].startswith("Ada met Bo⟪")
        return json.dumps(
            {
                "id": item["id"],
                "search": "博。",
                "replacement": f"博{payload['markers'][0]}。",
            }
        )

    client = FakeClient(handler=respond)
    config = Config.from_dict({"llm": {"preset": "fake"}})
    AnnotationService(PipelineRuntime(config, client=client)).align_annotations_after_batch(
        0, chapter, 0, 1, project
    )
    target = chapter.segments[0].target
    row = annotations_from_store(project, {"0:0": target})["0:0"]["placements"][0]
    assert (row["target_start"], row["target_end"]) == (6, 6)
    assert row["method"] == "llm_local_patch"
    assert target == "阿达见到了博。"


def test_pdf_client_sends_exact_alignment_payload(tmp_path, monkeypatch):
    client = BabeldocBridgeClient("http://bridge.invalid")
    monkeypatch.setattr(client, "health", lambda: {"capabilities": ["annotation-best-effort-v1"]})
    requests = []

    def post(*args, **kwargs):
        requests.append(kwargs["json"])
        return SimpleNamespace(status_code=200, content=b"%PDF synthetic result")

    monkeypatch.setattr("wenyi_core.pdf_bridge.client.httpx.post", post)
    payload = {"0:0": {"target_digest": "digest", "placements": []}}
    client.fillback("session", {"0:0": "正文"}, out_path=tmp_path / "out.pdf", annotations=payload)
    assert requests == [
        {
            "session_id": "session",
            "translations": {"0:0": "正文"},
            "annotations": payload,
        }
    ]


def test_pdf_client_logs_bridge_statistics_without_changing_export(tmp_path, monkeypatch, caplog):
    client = BabeldocBridgeClient("http://bridge.invalid")
    monkeypatch.setattr(client, "health", lambda: {"capabilities": ["annotation-best-effort-v1"]})
    statistics = {"total": 2, "aligned": 1, "applied": 1, "skipped": 1}
    response = SimpleNamespace(
        status_code=200,
        content=b"%PDF translated output",
        headers={"X-Wenyi-PDF-Annotation-Stats": json.dumps(statistics)},
    )
    monkeypatch.setattr("wenyi_core.pdf_bridge.client.httpx.post", lambda *a, **kw: response)
    with caplog.at_level("INFO", logger="wenyi_core.pdf_bridge.client"):
        client.fillback("session", {"0:0": "中文译文"}, out_path=tmp_path / "out.pdf")
    assert (tmp_path / "out.pdf").read_bytes() == response.content
    assert "PDF annotation statistics:" in caplog.text
    assert '"skipped": 1' in caplog.text
