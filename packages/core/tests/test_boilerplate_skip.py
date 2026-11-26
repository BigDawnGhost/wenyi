"""Tests for optional ingest-time scraped-site boilerplate skipping (issue #20)."""

from __future__ import annotations

import zipfile

import pytest
from docx import Document as DocxDocument
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.ingest.boilerplate import (
    compile_boilerplate_patterns,
    mark_boilerplate_segments,
    match_boilerplate,
)
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.prepare.input_preparation import parse_document
from wenyi_core.storage.file import FileStorage


def _write_test_epub(path, body_html: str) -> None:
    container = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles>"
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="uid">boilerplate-test</dc:identifier>'
        "<dc:title>Test Novel</dc:title>"
        "<dc:language>zh</dc:language>"
        "</metadata>"
        "<manifest>"
        '<item id="ch1" href="chap_01.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest>"
        '<spine><itemref idref="ch1"/></spine>'
        "</package>"
    )
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Chapter 1</title></head>"
        f"<body><h1>第一章</h1>{body_html}</body></html>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/chap_01.xhtml", chapter)


@pytest.mark.parametrize(
    "text,expected_prefix",
    [
        ("记住我们看书网", "builtin:zh_site_watermark"),
        ("请...您....收藏_6191书1吧…", "builtin:zh_site_watermark"),
        ("最新章节请访问笔趣阁 www.example.com", "builtin:zh_site_watermark"),
        ("Ads by Pubfuture", "builtin:ad_network_marker"),
        ("Ad by Google", "builtin:ad_network_marker"),
        (">", "builtin:scraper_stray_fragment"),
    ],
)
def test_builtin_boilerplate_rules_match_known_artifacts(text: str, expected_prefix: str):
    assert match_boilerplate(text) == expected_prefix


@pytest.mark.parametrize(
    "text",
    [
        "He complained about the ads by the highway all night.",
        "Ads by the road lit up the rain.",
        '"Did you see the ads by the station?" she asked.',
        "39",
        "Chapter 39",
        "She counted 39 steps to the gate.",
        "记住我们的约定，明晚在桥头见面。",
        "请帮我收藏好这封信，不要弄丢了。",
        "...",
        "——",
        "***",
        "A normal paragraph of story text.",
        # A bare URL can be a bibliography entry or a link a technical book prints on purpose.
        "https://doi.org/10.1016/S0140-6736(20)30183-5",
        "https://www.biquge.example/book/123",
        "[www.example.org]",
        # Trailing-off dialogue and interjections are story text, not scraper debris.
        "...No",
        "...Oh",
        "?!Ah",
        "……嗯",
        "，.??m",
    ],
)
def test_builtin_boilerplate_rules_avoid_false_positives(text: str):
    assert match_boilerplate(text) is None


def test_custom_boilerplate_patterns_match_and_validate():
    compiled = compile_boilerplate_patterns([r"^\d{1,3}$", r"Sponsored content"])
    assert match_boilerplate("39", custom_patterns=compiled) == "custom:0"
    assert match_boilerplate("Sponsored content here", custom_patterns=compiled) == "custom:1"
    assert match_boilerplate("Normal sentence 39.", custom_patterns=compiled) is None

    with pytest.raises(ValueError, match="Invalid regex"):
        Config.model_validate({"pipeline": {"boilerplate_patterns": ["[unclosed"]}})


def test_ingest_skip_boilerplate_preserves_indices_and_anchors_and_excludes_from_all_exports(
    tmp_path,
):
    epub_path = tmp_path / "scraped.epub"
    _write_test_epub(
        epub_path,
        "<p>第一段正文故事。</p>"
        "<p>记住我们看书网</p>"
        "<p>Ads by Pubfuture</p>"
        "<p>好书尽在www.qinkan.net</p>"
        "<p>39</p>"
        "<p>&gt;</p>"
        "<p>第二段正文故事。</p>",
    )

    # Default off: nothing skipped.
    cfg_default = Config.model_validate(
        {
            "source_lang": "zh",
            "target_lang": "en",
            "state_dir": str(tmp_path / "state-off"),
            "llm": {"preset": "fake"},
            "pipeline": {"review": False, "polish": False, "book_understanding": False},
        }
    )
    store_off = FileStorage(str(tmp_path / "state-off" / "run"))
    doc_off = parse_document(store_off, str(epub_path), cfg_default)
    ch_off = doc_off.chapters[0]
    assert all(not seg.meta.get("skip") for seg in ch_off.segments)
    assert "skipped_boilerplate_count" not in doc_off.meta

    # Enabled with built-in rules + optional user regex for lone short numbers.
    cfg_on = Config.model_validate(
        {
            "source_lang": "zh",
            "target_lang": "en",
            "state_dir": str(tmp_path / "state-on"),
            "llm": {"preset": "fake"},
            "pipeline": {
                "review": False,
                "polish": False,
                "book_understanding": False,
                "skip_boilerplate": True,
                "boilerplate_patterns": [r"^\d{1,3}$"],
            },
        }
    )
    store_on = FileStorage(str(tmp_path / "state-on" / "run"))
    doc_on = parse_document(store_on, str(epub_path), cfg_on)
    ch_on = doc_on.chapters[0]

    # Segment count, indices, and EPUB backfill anchors are completely preserved.
    assert len(ch_on.segments) == len(ch_off.segments)
    assert [s.index for s in ch_on.segments] == [s.index for s in ch_off.segments]
    assert [s.anchor for s in ch_on.segments] == [s.anchor for s in ch_off.segments]
    assert doc_on.meta["skipped_boilerplate_count"] == 5
    assert [s.source for s in ch_on.text_segments] == [
        "第一章",
        "第一段正文故事。",
        "第二段正文故事。",
    ]

    # Translate through Orchestrator and verify skipped segments are never translated
    # and never appear in any exported format (mono or bilingual).
    seen_inputs: list[str] = []

    def respond(messages, tier, json_mode):
        import json
        import re

        system = messages[0]["content"]
        user = messages[-1]["content"]
        seen_inputs.append(user)
        if json_mode:
            if '"titles"' in system or "chapter titles" in system.lower():
                count = len(re.findall(r"^\[\d+\] ", user, re.MULTILINE))
                return json.dumps({"titles": [f"Title {i}" for i in range(max(1, count))]})
            if '"terms"' in system:
                return json.dumps({"terms": []})
            if '"translations"' in system:
                matches = re.findall(r"^\[\d+\] (.*)$", user, re.MULTILINE)
                return json.dumps({"translations": [f"EN:{rest}" for rest in matches]})
            return json.dumps(
                {"genre": "fiction", "tone": "neutral", "narrative_pov": "third", "notes": ""}
            )
        return "EN:translated"

    orch = Orchestrator(cfg_on, client=FakeClient(handler=respond), storage=store_on)
    orch.run_steps(str(epub_path), {"translate"})

    joined_inputs = "\n".join(seen_inputs)
    for junk in ("记住我们看书网", "Ads by Pubfuture", "好书尽在www.qinkan.net"):
        assert junk not in joined_inputs

    # Verify initialization event recorded skipped count.
    init_events = [e for e in store_on.list_events() if e.get("event") == "run_initialized"]
    assert init_events and init_events[0]["skipped_boilerplate_count"] == 5

    # Export to EPUB, DOCX, HTML, TXT, Markdown (mono and bilingual) and verify absence.
    for fmt, ext in (
        ("epub", ".epub"),
        ("docx", ".docx"),
        ("html", ".html"),
        ("txt", ".txt"),
        ("markdown", ".md"),
    ):
        for bilingual in (False, True):
            out_path = str(tmp_path / f"out-{fmt}-{'bi' if bilingual else 'mono'}{ext}")
            assemble(
                store_on, str(epub_path), out_path=out_path, out_format=fmt, bilingual=bilingual
            )
            if fmt == "epub":
                with zipfile.ZipFile(out_path) as zf:
                    body = "\n".join(
                        zf.read(name).decode("utf-8", errors="ignore")
                        for name in zf.namelist()
                        if name.endswith(".xhtml") and "chap_01" in name
                    )
            elif fmt == "docx":
                docx_doc = DocxDocument(out_path)
                body = "\n".join(p.text for p in docx_doc.paragraphs)
            else:
                body = (tmp_path / f"out-{fmt}-{'bi' if bilingual else 'mono'}{ext}").read_text(
                    encoding="utf-8"
                )
            assert "EN:第一段正文故事。" in body
            assert "EN:第二段正文故事。" in body
            for junk in ("记住我们看书网", "Ads by Pubfuture", "好书尽在www.qinkan.net"):
                assert junk not in body, f"Found {junk!r} in {fmt} (bilingual={bilingual})"
            if fmt in {"txt", "markdown", "docx"}:
                assert "\n39\n" not in f"\n{body}\n"
                assert "\n>\n" not in f"\n{body}\n"
            else:
                assert ">39<" not in body
                assert ">&gt;<" not in body


def test_mark_boilerplate_segments_is_noop_when_disabled(tmp_path):
    from wenyi_core.models.document import Chapter, Document, Segment

    doc = Document(
        title="Sample",
        source_lang="zh",
        target_lang="en",
        fmt="text",
        chapters=[
            Chapter(
                index=0,
                title="Ch 1",
                segments=[Segment(index=0, source="Ads by Pubfuture")],
            )
        ],
    )
    assert mark_boilerplate_segments(doc, enabled=False) == []
    assert not doc.chapters[0].segments[0].meta.get("skip")


def test_parse_cache_key_tracks_builtin_boilerplate_rules():
    from wenyi_core.config import Config
    from wenyi_core.ingest.boilerplate import BUILTIN_RULES_VERSION
    from wenyi_core.pipeline.prepare.input_preparation import ingest_config

    cfg = Config.from_dict({"pipeline": {"skip_boilerplate": True}})
    assert ingest_config(cfg)["boilerplate_rules_version"] == BUILTIN_RULES_VERSION
    assert "boilerplate_rules_version" not in ingest_config(Config())
