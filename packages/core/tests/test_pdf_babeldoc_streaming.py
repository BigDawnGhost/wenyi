"""Whole-book BabelDOC extracts are consumed incrementally.

The bridge returns one decoded JSON payload per book. These tests pin that
read_pdf_babeldoc streams the paragraph list into the document (freeing
each raw payload as it is converted) instead of keeping the whole extract
alive alongside the assembled chapters. Small synthetic books and a fake
bridge client only; no real BabelDOC service and no timing assertions.
"""

from __future__ import annotations

import json
import weakref
from unittest.mock import Mock

import pytest
from wenyi_core.ingest import pdf_babeldoc
from wenyi_core.ingest.pdf_babeldoc import read_pdf_babeldoc

from tests.pdf_fixtures import pdf_bytes

PARAGRAPH_COUNT = 400
PAGE_COUNT = 20


class TrackedParagraph(dict):
    """Paragraph dict that supports weak references for liveness assertions."""


def synthetic_payload(paragraph_count: int = PARAGRAPH_COUNT, page_count: int = PAGE_COUNT):
    paragraphs = [
        TrackedParagraph(
            {
                "id": f"{index % page_count}:{index}",
                "page": index % page_count,
                "source": f"Paragraph {index} body text.",
                "layout_label": "plain text",
            }
        )
        for index in range(paragraph_count)
    ]
    return {"session_id": "session-1", "paragraphs": {"paragraphs": paragraphs}}


@pytest.fixture
def synthetic_pdf(tmp_path):
    pdf = tmp_path / "synthetic-book.pdf"
    pdf.write_bytes(pdf_bytes(["Source text"]))
    return pdf


@pytest.fixture
def bridge(monkeypatch):
    client = Mock()
    monkeypatch.setattr(pdf_babeldoc, "BabeldocBridgeClient", client)
    return client.return_value


def test_whole_book_extract_is_released_while_document_is_built(synthetic_pdf, bridge):
    payload = synthetic_payload()
    bridge.extract.return_value = payload

    document = read_pdf_babeldoc(str(synthetic_pdf), "en", "zh", bridge_url="http://bridge.test")

    bridge.extract.assert_called_once_with(str(synthetic_pdf), pages=None)
    raw_paragraphs = payload["paragraphs"]["paragraphs"]
    assert len(raw_paragraphs) == PARAGRAPH_COUNT
    assert all(slot is None for slot in raw_paragraphs)
    assert len(document.chapters) == 1
    assert [segment.source for segment in document.chapters[0].segments] == [
        f"Paragraph {index} body text." for index in range(PARAGRAPH_COUNT)
    ]
    assert document.meta["babeldoc_session_id"] == "session-1"


def test_paragraph_payloads_are_freed_during_chapter_build(synthetic_pdf, bridge, monkeypatch):
    payload = synthetic_payload()
    raw_paragraphs = payload["paragraphs"]["paragraphs"]
    refs = [weakref.ref(para) for para in raw_paragraphs]
    bridge.extract.return_value = payload
    max_stale = 0
    real_segments_from_para = pdf_babeldoc._segments_from_para

    def tracked_segments_from_para(index, para):
        nonlocal max_stale
        stale = sum(1 for ref in refs[:index] if ref() is not None)
        max_stale = max(max_stale, stale)
        return real_segments_from_para(index, para)

    monkeypatch.setattr(pdf_babeldoc, "_segments_from_para", tracked_segments_from_para)

    document = read_pdf_babeldoc(str(synthetic_pdf), "en", "zh", bridge_url="http://bridge.test")

    # Every earlier paragraph is already freed when the next one is converted,
    # so peak memory stays bounded by the in-flight payload, not the whole book.
    assert max_stale == 0
    assert all(ref() is None for ref in refs)
    assert len(document.chapters[0].segments) == PARAGRAPH_COUNT


def test_toc_chapters_are_built_from_the_released_stream(synthetic_pdf, bridge, monkeypatch):
    payload = synthetic_payload(paragraph_count=40, page_count=4)
    bridge.extract.return_value = payload

    class FakeInspection:
        def __init__(self, path):
            self.path = path

        def is_image_only(self, pages=None):
            return False

        def chapter_starts(self):
            return [(0, "Alpha"), (2, "Beta")]

    monkeypatch.setattr(pdf_babeldoc, "_PdfInspection", FakeInspection)

    document = read_pdf_babeldoc(str(synthetic_pdf), "en", "zh", bridge_url="http://bridge.test")

    assert [(chapter.title, chapter.meta["pdf_toc_page"]) for chapter in document.chapters] == [
        ("Alpha", 0),
        ("Beta", 2),
    ]
    assert [len(chapter.segments) for chapter in document.chapters] == [20, 20]
    assert all(slot is None for slot in payload["paragraphs"]["paragraphs"])
    assert document.meta["pdf_toc_chapters"] == 2


def test_cache_keeps_the_complete_payload_before_release(synthetic_pdf, bridge, tmp_path):
    payload = synthetic_payload(paragraph_count=50, page_count=5)
    bridge.extract.return_value = payload
    cache_dir = tmp_path / "cache"

    read_pdf_babeldoc(
        str(synthetic_pdf), "en", "zh", bridge_url="http://bridge.test", cache_dir=str(cache_dir)
    )

    saved = json.loads((cache_dir / "babeldoc_extract.json").read_text(encoding="utf-8"))
    expected = synthetic_payload(paragraph_count=50, page_count=5)["paragraphs"]["paragraphs"]
    assert saved["session_id"] == "session-1"
    assert saved["paragraphs"]["paragraphs"] == expected


def test_non_dict_paragraph_slots_are_skipped_and_released(synthetic_pdf, bridge):
    payload = synthetic_payload(paragraph_count=6, page_count=2)
    raw_paragraphs = payload["paragraphs"]["paragraphs"]
    raw_paragraphs[1] = "corrupted entry"
    raw_paragraphs[4] = None
    bridge.extract.return_value = payload

    document = read_pdf_babeldoc(str(synthetic_pdf), "en", "zh", bridge_url="http://bridge.test")

    assert all(slot is None for slot in raw_paragraphs)
    sources = [segment.source for segment in document.chapters[0].segments]
    assert sources == [f"Paragraph {index} body text." for index in (0, 2, 3, 5)]
