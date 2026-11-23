"""Optional retroactive glossary pass over completed chapters (pipeline.glossary_retroactive)."""

from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.translation import TranslationService
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler


def _config(tmp_path, retroactive=False):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "segment": {"max_tokens_per_batch": 1, "max_tokens_per_segment": 0},
            "pipeline": {
                "review": False,
                "polish": False,
                "book_understanding": False,
                "annotation_alignment": False,
                "align_retry_limit": 1,
                "glossary_retroactive": retroactive,
            },
        }
    )


def _store_with_done_chapters(tmp_path):
    store = FileStorage(str(tmp_path / "run"))
    document = Document(
        title="Book",
        source_lang="en",
        target_lang="zh",
        fmt="text",
        chapters=[
            Chapter(
                index=0,
                title="Outset",
                segments=[
                    Segment(index=0, source="Alice waited by the window.", target="旧译0"),
                    Segment(index=1, source="The door opened slowly.", target="旧译1"),
                ],
            ),
            Chapter(
                index=1,
                title="Reply",
                segments=[
                    Segment(index=0, source="A letter arrived after dinner.", target="旧译0")
                ],
            ),
        ],
    )
    manifest = store.stage_document(document, source_hash="a" * 64)
    manifest["initialized"] = True
    store.save_manifest(manifest)
    store.set_chapter_status(0, "done")
    store.set_chapter_status(1, "done")
    return store


def _service(tmp_path, retroactive=False):
    config = _config(tmp_path, retroactive=retroactive)
    orchestrator = Orchestrator(config, client=FakeClient(handler=routing_handler))
    return orchestrator._translation


def test_retroactive_disabled_by_default(tmp_path, monkeypatch):
    store = _store_with_done_chapters(tmp_path)
    service = _service(tmp_path)
    calls = []
    monkeypatch.setattr(
        service, "retroactive_glossary_pass", lambda *args, **kwargs: calls.append(1) or 0
    )
    try:
        service.run(store, book_synopsis="")
        assert calls == []
        assert store.load_chapter(0).segments[0].target == "旧译0"
        assert store.list_events(event_type="glossary_retroactive_chapter", limit=0) == []
    finally:
        store.close()


def test_run_hook_invokes_pass_when_enabled(tmp_path, monkeypatch):
    store = _store_with_done_chapters(tmp_path)
    service = _service(tmp_path, retroactive=True)
    calls = []
    monkeypatch.setattr(
        service,
        "retroactive_glossary_pass",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 0,
    )
    try:
        service.run(store, book_synopsis="")
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] is store
        assert args[1] is store
    finally:
        store.close()


def test_retroactive_pass_retranslates_matching_chapters(tmp_path):
    store = _store_with_done_chapters(tmp_path)
    store.upsert_term(GlossaryTerm(source="Alice", target="爱丽丝"))
    store.upsert_term(GlossaryTerm(source="letter", target="信"))
    service = _service(tmp_path)
    try:
        assert service.retroactive_glossary_pass(store, store, style="") == 2
        assert [s.target for s in store.load_chapter(0).segments] == ["译0", "译0"]
        assert store.load_chapter(1).segments[0].target == "译0"
        retro_batches = [
            e
            for e in store.list_events(event_type="batch_translated", limit=0)
            if e.get("retroactive")
        ]
        assert len(retro_batches) == 3
        events = store.list_events(event_type="glossary_retroactive_chapter", limit=0)
        assert sorted(e["chapter"] for e in events) == [0, 1]
        assert len({e["fingerprint"] for e in events}) == 1
        alice_term = store.get_term("Alice")
        letter_term = store.get_term("letter")
        assert alice_term is not None and letter_term is not None
        assert alice_term.target == "爱丽丝"
        assert letter_term.target == "信"
    finally:
        store.close()


def test_fingerprint_skips_unchanged_snapshot_and_new_term_retriggers(tmp_path):
    store = _store_with_done_chapters(tmp_path)
    store.upsert_term(GlossaryTerm(source="Alice", target="爱丽丝"))
    service = _service(tmp_path)
    try:
        assert service.retroactive_glossary_pass(store, store, style="") == 1
        assert service.retroactive_glossary_pass(store, store, style="") == 0
        store.upsert_term(GlossaryTerm(source="letter", target="信"))
        assert service.retroactive_glossary_pass(store, store, style="") == 2
        assert service.retroactive_glossary_pass(store, store, style="") == 0
        events = store.list_events(event_type="glossary_retroactive_chapter", limit=0)
        chapter_fingerprints = {}
        for event in events:
            chapter_fingerprints.setdefault(event["chapter"], []).append(event["fingerprint"])
        assert len(chapter_fingerprints[0]) == 2
        assert len(set(chapter_fingerprints[0])) == 2
        assert len(chapter_fingerprints[1]) == 1
    finally:
        store.close()


def test_chapters_without_matching_terms_are_skipped(tmp_path):
    store = _store_with_done_chapters(tmp_path)
    store.upsert_term(GlossaryTerm(source="raven", target="渡鸦"))
    service = _service(tmp_path)
    try:
        assert service.retroactive_glossary_pass(store, store, style="") == 0
        assert store.load_chapter(0).segments[0].target == "旧译0"
        assert store.load_chapter(1).segments[0].target == "旧译0"
        assert store.list_events(event_type="glossary_retroactive_chapter", limit=0) == []
    finally:
        store.close()


def test_glossary_fingerprint_covers_term_fields():
    base = GlossaryTerm(source="Alice", target="爱丽丝")
    assert TranslationService.glossary_fingerprint(
        [base]
    ) == TranslationService.glossary_fingerprint([GlossaryTerm(source="Alice", target="爱丽丝")])
    assert TranslationService.glossary_fingerprint(
        [base]
    ) != TranslationService.glossary_fingerprint(
        [GlossaryTerm(source="Alice", target="爱丽丝", note="heroine")]
    )
    assert TranslationService.glossary_fingerprint(
        [base]
    ) != TranslationService.glossary_fingerprint(
        [GlossaryTerm(source="Alice", target="爱丽丝", aliases=["Alicia"])]
    )
    assert TranslationService.glossary_fingerprint(
        [base]
    ) != TranslationService.glossary_fingerprint(
        [GlossaryTerm(source="Alice", target="爱丽丝"), GlossaryTerm(source="letter", target="信")]
    )
