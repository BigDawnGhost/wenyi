"""Source-language reading boundaries exercised through offline model requests."""

import copy
import json
from dataclasses import asdict

import pytest
from wenyi_core.agents.review_fixer import ReviewFixer
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.glossary.extractor import GlossaryExtractor
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.precision import PrecisionBatchExecutor
from wenyi_core.pipeline.precision_records import load_precision_call
from wenyi_core.pipeline.runtime import PipelineRuntime
from wenyi_core.pipeline.title_translation import TitleTranslationService
from wenyi_core.pipeline.translation_batch import BatchPlan
from wenyi_core.review.evidence import BookEvidenceIndex
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.precision_archive import PrecisionArchive


def config(source, target):
    return Config.from_dict(
        {"language": {"source": source, "target": target}, "llm": {"preset": "fake"}}
    )


@pytest.mark.parametrize("source,target", [("ja", "en"), ("en", "ja"), ("ru", "ja"), ("zh", "ja")])
def test_translation_request_filters_reading_by_source(source, target):
    term = GlossaryTerm("Name", "Translated", reading="UNIQUE_READING")
    original = asdict(term)
    client = FakeClient(handler=lambda *_: json.dumps({"translations": ["Translated text"]}))
    Translator(client, config(source, target)).translate_batch(
        ["Name appears"], glossary_terms=[term]
    )
    request = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert ("UNIQUE_READING" in request) == (source == "ja")
    assert ("Pronunciation:" in request) == (source == "ja")
    assert asdict(term) == original


@pytest.mark.parametrize("source", ["ja", "en", "ru", "zh"])
def test_extractor_ignores_non_japanese_model_reading(source):
    term = GlossaryTerm("Name", "Translated", reading="EXISTING_READING")
    original = asdict(term)
    client = FakeClient(
        handler=lambda *_: json.dumps(
            {"terms": [{"source": "New", "target": "New translation", "reading": "MODEL_READING"}]}
        )
    )
    extracted = GlossaryExtractor(client, config(source, "en" if source == "ja" else "ja")).extract(
        "Name and New", "Translated and New translation", [term]
    )
    assert extracted[0].reading == ("MODEL_READING" if source == "ja" else "")
    request = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert ("EXISTING_READING" in request) == (source == "ja")
    assert asdict(term) == original


@pytest.mark.parametrize("source", ["ja", "en", "ru", "zh"])
def test_review_evidence_filters_both_term_tools_without_mutation(source):
    term = GlossaryTerm("Name", "Translated", reading="EXISTING_READING")
    original = copy.deepcopy(term)
    evidence = BookEvidenceIndex([], [term], {}, source_lang=source)
    for tool in ("glossary_term", "term_occurrences"):
        result = getattr(evidence, tool)({"term": "Name"})
        payload = result["glossary_term"] if tool == "term_occurrences" else result["term"]
        assert ("reading" in payload) == (source == "ja")
        if source == "ja":
            assert payload["reading"] == term.reading
    assert term == original


@pytest.mark.parametrize("source", ["ja", "en", "ru", "zh", "auto"])
def test_runtime_requests_and_precision_replay_use_resolved_source(tmp_path, source):
    """Exercise runtime language refresh, title service and the real four-call archive path."""
    resolved = "ja" if source == "auto" else source
    cfg = config(source, "en" if resolved == "ja" else "ja")
    term = GlossaryTerm("Name", "Translated", reading="UNIQUE_READING")
    original = asdict(term)

    def handler(messages, *_):
        if "Task (JSON):" in messages[-1]["content"]:
            return '{"translations": ["Translated text"]}'
        return json.dumps(
            {
                "translations": ["Translated text"],
                "polished": ["Polished text"],
                "titles": ["Translated title"],
                "reviewed_segments": 1,
                "complete": True,
                "issues": [],
                "terms": [],
            }
        )

    client = FakeClient(handler=handler)
    runtime = PipelineRuntime(cfg, client)
    runtime.apply_language(resolved)
    runtime.translator.translate_batch(["Name appears"], glossary_terms=[term])
    runtime.polisher.polish(["Translated text"], glossary_terms=[term])
    runtime.reviewer.review(["Name appears"], ["Translated text"], [term])
    runtime.extractor.extract("Name appears", "Translated text", [term])
    store = FileStorage(str(tmp_path / "book"))
    store.save_chapter(Chapter(index=0, segments=[Segment(index=0, source="Name appears")]))
    store.save_manifest(
        {
            "title": "Name title",
            "source_lang": resolved,
            "target_lang": cfg.target_lang,
            "source_sha256": "0" * 64,
            "chapters": [{"index": 0, "title": "Name chapter"}],
        }
    )
    store.upsert_term(term)
    TitleTranslationService(runtime.title_translator).run(store, store)
    plan = BatchPlan.capture(
        0, 0, store.load_chapter(0).text_segments, [term], "", "", "", "", [[]], ""
    )
    result = PrecisionBatchExecutor(client, runtime.config).execute(plan, store)
    archive = PrecisionArchive(store)  # Replay must not depend on today's source/rendering.
    metadata = store.read_artifact(f"{result.precision_key}/meta.json")
    assert metadata is not None
    glossary_ref = metadata["plan"]["glossary_ref"]
    assert archive.load_glossary(glossary_ref) == (term,)
    assert ("UNIQUE_READING" in archive.glossary_text(glossary_ref)) == (resolved == "ja")
    call_keys = [
        key
        for key in store.list_artifacts(f"{result.precision_key}/calls/")
        if key.endswith(".json")
    ]
    assert len(call_keys) == 4
    for key in call_keys:
        restored = load_precision_call(store, key)
        assert any(restored["messages"] == call["messages"] for call in client.calls)
    assert len(client.calls) == 9
    for call in client.calls:
        request = json.dumps(call["messages"], ensure_ascii=False)
        assert ("UNIQUE_READING" in request) == (resolved == "ja")
    saved_term = store.get_term("Name")
    assert saved_term is not None
    assert saved_term.reading == "UNIQUE_READING"
    assert asdict(term) == original


@pytest.mark.parametrize("source", ["ja", "en", "ru", "zh"])
def test_review_fixer_structured_requests_and_legacy_text(source):
    term = GlossaryTerm("Name", "Translated", reading="UNIQUE_READING")
    original = asdict(term)
    cfg = config(source, "en" if source == "ja" else "ja")
    target = "Translated text"
    response = {
        "complete": True,
        "segment_ref": "c0:s0",
        "before_hash": ReviewFixer.target_hash(target),
        "issue_ids": ["issue"],
        "replacement": "Revised text",
    }
    client = FakeClient(handler=lambda *_: json.dumps(response))
    fixer = ReviewFixer(client, cfg)
    issue = {
        "issue_id": "issue",
        "chapter": 0,
        "index": 0,
        "type": "term",
        "detail": "Incorrect term",
        "suggestion": "Use the glossary target",
    }
    fixer.propose(1, "c0:s0", 0, 0, "Name appears", target, [issue], relevant_glossary=[term])
    request = json.dumps(client.calls[0]["messages"])
    assert ("UNIQUE_READING" in request) == (source == "ja")
    assert asdict(term) == original
    legacy = "Caller-owned prose: Pronunciation: keep this literal text."
    fixer.propose(1, "c0:s0", 0, 0, "Name appears", target, [issue], relevant_glossary=legacy)
    assert legacy in client.calls[1]["messages"][-1]["content"]


def test_extractor_does_not_clear_manual_database_reading(tmp_path):
    store = FileStorage(str(tmp_path / "book"))
    store.upsert_term(GlossaryTerm("Name", "Translated", reading="MANUAL_READING"))
    client = FakeClient(
        handler=lambda *_: (
            '{"terms": [{"source": "Name", "target": "Translated", "reading": "NEW"}]}'
        )
    )
    GlossaryExtractor(client, config("en", "ja")).extract_and_store(
        store, "Name appears", "Translated text", 0
    )
    saved_term = store.get_term("Name")
    assert saved_term is not None
    assert saved_term.reading == "MANUAL_READING"
