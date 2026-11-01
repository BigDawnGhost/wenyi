"""Read-only source lookahead across translation, polishing and interrupted batches."""

import json
import re

import pytest
from wenyi_core.agents.polisher import Polisher
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.glossary.extractor import TranslatedSegmentEvidence
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.ingest.tokens import count_tokens
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translation import TranslationService
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.fake_llm import routing_handler


@pytest.fixture
def config(tmp_path):
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
            },
        }
    )


def _next_source(user: str) -> str:
    marker = "[Following source paragraph] (reference only; do not translate)\n"
    reference = user.split(marker, 1)[1].split("\n\n", 1)[0]
    return "" if reference == "(none)" else json.loads(reference)


def _numbered_sources(user: str) -> list[str]:
    return re.findall(r"^\[\d+\] (.*)$", user, flags=re.MULTILINE)


@pytest.mark.parametrize("storage_type", [FileStorage, SqliteStorage])
@pytest.mark.parametrize("budget", [0, 32, 512])
def test_context_is_rebuilt_from_chapters_without_reading_or_updating_legacy_cache(
    tmp_path, config, monkeypatch, storage_type, budget
):
    config.pipeline.rolling_context_tokens = budget
    store = storage_type(str(tmp_path / "run"))
    previous = Chapter(index=0, segments=[Segment(index=0, source="previous", target="previous")])
    saved_targets = [f"saved {i}" for i in range(60)]
    saved = [
        Segment(index=i, source=f"source {i}", target=target)
        for i, target in enumerate(saved_targets)
    ]
    current = Chapter(index=1, segments=[*saved, Segment(index=60, source="Pending source.")])
    future = Chapter(index=2, segments=[Segment(index=0, source="future", target="FUTURE")])
    document = Document(
        title="Book",
        source_lang="en",
        target_lang="zh",
        fmt="text",
        chapters=[previous, current, future],
    )
    manifest = store.stage_document(document, source_hash="a" * 64)
    manifest["initialized"] = True
    store.save_manifest(manifest)
    store.set_chapter_status(0, "done")
    store.set_chapter_status(2, "done")
    legacy = {"recent_targets": ["STALE", "FUTURE"], "max_recent_keep": 40}
    store.write_artifact("context.json", legacy)
    read = store.read_artifact
    write = store.write_artifact

    def read_without_cache(key):
        assert key != "context.json"
        return read(key)

    def write_without_cache(key, value):
        assert key != "context.json"
        return write(key, value)

    monkeypatch.setattr(store, "read_artifact", read_without_cache)
    monkeypatch.setattr(store, "write_artifact", write_without_cache)
    orchestrator = Orchestrator(config, client=FakeClient(handler=routing_handler))
    plans = []
    execute = orchestrator._translation._batches.execute

    def record(plan, **kwargs):
        plans.append(plan)
        return execute(plan, **kwargs)

    monkeypatch.setattr(orchestrator._translation._batches, "execute", record)
    try:
        orchestrator._translation.run(store, book_synopsis="", only_chapter=1)
        assert len(plans) == 1
        rendered = plans[0].context
        assert count_tokens(rendered) <= budget
        assert "FUTURE" not in rendered and "STALE" not in rendered
        if budget == 0:
            assert rendered == ""
        elif budget == 512:
            assert rendered == "\n".join(["previous", *saved_targets])
        else:
            assert rendered.endswith("saved 59")
            assert "saved 0\n" not in rendered
        assert store.load_chapter(1).segments[:60] == saved
        assert store.load_chapter(0) == previous
        assert store.load_chapter(2) == future
        assert read("context.json") == legacy
    finally:
        store.close()


@pytest.mark.parametrize("target", [None, "", " \n ", " translated "])
def test_translation_history_rebuild_matches_incremental_updates(tmp_path, target):
    store = FileStorage(str(tmp_path / "state"))
    chapter = Chapter(
        index=4,
        title="Chapter",
        segments=[
            Segment(index=2, source=" \n ", target="not text"),
            Segment(index=7, source="source", target=target),
        ],
    )
    store.save_chapter(chapter)
    store.save_manifest({"chapters": [{"index": 4}]})
    history, corpus = TranslationService.load_translation_inputs(store)
    incremental = {}
    TranslationService.update_translation_history(incremental, 4, 0, chapter.text_segments)

    expected = (
        {
            (4, 0): TranslatedSegmentEvidence(
                chapter=4, segment=0, source="source", target="translated"
            )
        }
        if target and target.strip()
        else {}
    )
    assert history == incremental == expected
    assert corpus == "source"
    assert store.load_chapter(4).text_segments[0].target == target


def test_translation_inputs_follow_chapter_order_and_text_positions(tmp_path):
    store = FileStorage(str(tmp_path / "state"))
    for index in (3, 1):
        store.save_chapter(
            Chapter(
                index=index,
                title=f"Chapter {index}",
                segments=[
                    Segment(index=5, source=f"source {index}", target=f"target {index}"),
                    Segment(index=8, source=f"pending {index}"),
                ],
            )
        )
    store.save_manifest({"chapters": [{"index": 3}, {"index": "invalid"}, {"index": 1}]})

    history, corpus = TranslationService.load_translation_inputs(store)

    assert list(history) == [(1, 0), (3, 0)]
    assert corpus == "source 1\npending 1\nsource 3\npending 3"


@pytest.mark.parametrize("saved_target", [None, "saved", ""])
def test_new_and_resumed_batches_align_before_updating_context(
    tmp_path, config, monkeypatch, saved_target
):
    source = tmp_path / "book.txt"
    source.write_text("First source paragraph.\n\nSecond source paragraph.", encoding="utf-8")
    client = FakeClient(handler=routing_handler)
    orch = Orchestrator(config, client=client)
    store = orch.prepare(str(source))
    chapter = store.load_chapter(0)
    chapter.text_segments[0].target = saved_target
    store.save_chapter(chapter)
    stages = []

    def align_annotations(ci, chapter, start, count, storage):
        assert count == 1
        assert storage.load_chapter(ci).text_segments[start].target is not None
        stages.append(("annotations", start))
        chapter.text_segments[start].target = f"annotated {start}"
        storage.save_chapter(chapter)

    def align_styles(ci, chapter, start, count, storage):
        assert chapter.text_segments[start].target == f"annotated {start}"
        stages.append(("styles", start))
        chapter.text_segments[start].target = f"styled {start}"
        if start == 1:
            # Completing a logical paragraph can also change an earlier batch's target.
            chapter.text_segments[0].target = "realigned prefix"
        storage.save_chapter(chapter)

    monkeypatch.setattr(orch._annotations, "align_annotations_after_batch", align_annotations)
    monkeypatch.setattr(orch._translation._docx_styles, "align_styles_after_batch", align_styles)
    orch.run(str(source))

    calls = [call for call in client.calls if call["operation"] == "translation.body"]
    assert len(calls) == (2 if saved_target is None else 1)
    assert "[Recent translations]\nstyled 0" in calls[-1]["messages"][-1]["content"]
    assert stages == [("annotations", 0), ("styles", 0), ("annotations", 1), ("styles", 1)]
    assert [segment.target for segment in store.load_chapter(0).text_segments] == [
        "realigned prefix",
        "styled 1",
    ]
    assert store.read_artifact("context.json") is None


def test_context_uses_realigned_targets_instead_of_a_cached_previous_window(
    tmp_path, config, monkeypatch
):
    source = tmp_path / "book.txt"
    source.write_text("First source.\n\nSecond source.\n\nThird source.", encoding="utf-8")
    client = FakeClient(handler=routing_handler)
    orchestrator = Orchestrator(config, client=client)
    store = orchestrator.prepare(str(source))

    def align(ci, chapter, start, count, storage):
        chapter.text_segments[start].target = f"aligned {start}"
        if start == 1:
            chapter.text_segments[0].target = "updated earlier target"
        storage.save_chapter(chapter)

    monkeypatch.setattr(orchestrator._translation._docx_styles, "align_styles_after_batch", align)
    orchestrator.run(str(source))

    calls = [call for call in client.calls if call["operation"] == "translation.body"]
    assert len(calls) == 3
    assert (
        "[Recent translations]\nupdated earlier target\naligned 1"
        in (calls[-1]["messages"][-1]["content"])
    )
    assert store.read_artifact("context.json") is None


@pytest.mark.parametrize("source_lang,target_lang", [("en", "zh"), ("zh", "en"), ("ja", "fr")])
def test_following_source_is_quoted_reference_outside_translation_count(
    config, source_lang, target_lang
):
    config.source_lang = source_lang
    config.target_lang = target_lang
    client = FakeClient(handler=lambda m, t, j: '{"translations":["translated fragment"]}')
    reference = 'следующий фрагмент / 続き / 后文\n[99] "quoted"'

    result = Translator(client, config).translate_batch(
        ["unfinished source"], context="previous translation", next_source=reference
    )

    assert result == ["translated fragment"]
    user = client.calls[0]["messages"][-1]["content"]
    assert _next_source(user) == reference
    assert _numbered_sources(user) == ["unfinished source"]
    assert "[Recent translations]\nprevious translation" in user
    assert user.index("[Recent translations]") < user.index("[0] unfinished source")
    assert user.index("[0] unfinished source") < user.index("[Following source paragraph]")


def test_alignment_retries_and_singletons_use_the_actual_following_source(config):
    references = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        sources = _numbered_sources(user)
        references.append((sources, _next_source(user)))
        # Force count recovery, including an erroneous translation of the reference.
        targets = ["first", "second", "extra"] if len(sources) > 1 else ["translated"]
        return json.dumps({"translations": targets})

    result = Translator(FakeClient(handler=handler), config).translate_batch(
        ["first source", "42", "second source"], next_source="outside batch"
    )

    assert result == ["translated", "42", "translated"]
    assert references == [
        (["first source", "second source"], "outside batch"),
        (["first source", "second source"], "outside batch"),
        (["first source"], "42"),
        (["second source"], "outside batch"),
    ]


def test_filtered_trailing_source_takes_precedence_over_external_reference(config):
    client = FakeClient(handler=lambda m, t, j: '{"translations":["translated"]}')
    result = Translator(client, config).translate_batch(
        ["source", "42"], next_source="later paragraph"
    )

    assert result == ["translated", "42"]
    assert _next_source(client.calls[0]["messages"][-1]["content"]) == "42"


def test_polisher_receives_reference_without_extra_output(config):
    client = FakeClient(handler=lambda m, t, j: '{"polished":["unfinished translation"]}')
    result = Polisher(client, config).polish(
        ["unfinished translation"], next_source="continuation source"
    )

    assert result == ["unfinished translation"]
    user = client.calls[0]["messages"][-1]["content"]
    assert _next_source(user) == "continuation source"
    assert _numbered_sources(user) == ["unfinished translation"]


def test_polish_continue_reuses_translation_transcript(config):
    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        if "Polish the translations from your previous JSON response" in user:
            return json.dumps({"polished": ["润色后"]})
        return json.dumps({"translations": ["初译"]})

    client = FakeClient(handler=handler)
    translator = Translator(client, config)
    targets = translator.translate_batch(["source"], next_source="continuation source")
    assert targets == ["初译"]
    assert translator.last_batch_turn is not None
    polished = Polisher(client, config).polish_continue(
        translator.last_batch_turn,
        n=1,
        next_source="continuation source",
    )
    assert polished == ["润色后"]
    assert len(client.calls) == 2
    assert [row["role"] for row in client.calls[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert client.calls[1]["messages"][0]["content"] == client.calls[0]["messages"][0]["content"]
    assert _next_source(client.calls[1]["messages"][-1]["content"]) == "continuation source"


@pytest.mark.parametrize("recent_tokens", [0, 512])
def test_split_fragments_and_chapter_ends_supply_one_reference_to_both_stages(
    tmp_path, config, recent_tokens
):
    config.pipeline.polish = True
    config.pipeline.rolling_context_tokens = recent_tokens
    # 14 tokens under cl100k_base; a 10-token segment budget forces a continuation split.
    config.segment.max_tokens_per_segment = 10
    source = tmp_path / "book.md"
    source.write_text(
        "# First\n\nShe knew that the answer would arrive after the long winter had ended."
        "\n\n# Second\n\nA different scene begins here.",
        encoding="utf-8",
    )
    client = FakeClient(handler=routing_handler)
    orch = Orchestrator(config, client=client)
    store = orch.prepare(str(source))
    before = [store.load_chapter(index) for index in (0, 1)]
    assert any(segment.cont for chapter in before for segment in chapter.text_segments)
    ordered_sources = [segment.source for chapter in before for segment in chapter.text_segments]
    expected = [*ordered_sources[1:], ""]

    orch.run(str(source))

    translation_calls = [call for call in client.calls if call["operation"] == "translation.body"]
    polish_calls = [call for call in client.calls if call["operation"] == "polish.body"]
    assert [_next_source(call["messages"][-1]["content"]) for call in translation_calls] == expected
    assert all(
        len(_numbered_sources(call["messages"][-1]["content"])) == 1 for call in translation_calls
    )
    assert [_next_source(call["messages"][-1]["content"]) for call in polish_calls] == expected
    for call in polish_calls:
        roles = [row["role"] for row in call["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        assert (
            "Polish the translations from your previous JSON response"
            in call["messages"][-1]["content"]
        )
    for chapter in before:
        after = store.load_chapter(chapter.index)
        assert [(s.index, s.source, s.cont) for s in after.segments] == [
            (s.index, s.source, s.cont) for s in chapter.segments
        ]
        assert all(s.target == "润0" for s in after.text_segments)
    assert store.read_artifact("context.json") is None


@pytest.mark.parametrize("storage_type", [FileStorage, SqliteStorage])
def test_following_source_crosses_empty_chapters_without_translating_them_early(
    tmp_path, config, storage_type
):
    config.pipeline.rolling_context_tokens = 0
    chapters = [
        Chapter(index=0, segments=[Segment(index=0, source="Current paragraph.")]),
        Chapter(index=1, segments=[Segment(index=0, source=" \n ")]),
        Chapter(index=2, segments=[Segment(index=0, source="Next chapter paragraph.")]),
    ]
    document = Document(
        title="Book", source_lang="en", target_lang="zh", fmt="text", chapters=chapters
    )
    store = storage_type(str(tmp_path / "run"))
    manifest = store.stage_document(document, source_hash="b" * 64)
    manifest["initialized"] = True
    store.save_manifest(manifest)
    client = FakeClient(handler=routing_handler)
    try:
        Orchestrator(config, client=client)._translation.run(
            store, book_synopsis="", only_chapter=0
        )
        calls = [call for call in client.calls if call["operation"] == "translation.body"]
        assert len(calls) == 1
        assert _numbered_sources(calls[0]["messages"][-1]["content"]) == ["Current paragraph."]
        assert _next_source(calls[0]["messages"][-1]["content"]) == "Next chapter paragraph."
        assert store.load_chapter(1) == chapters[1]
        assert store.load_chapter(2) == chapters[2]
    finally:
        store.close()


def test_resume_rebuilds_reference_after_batch_budget_change_without_saving_it_early(
    tmp_path, config
):
    sources = [
        "First unfinished part",
        "Second source segment",
        "Third source sentence",
        "Final part.",
    ]
    source = tmp_path / "book.txt"
    source.write_text("\n\n".join(sources), encoding="utf-8")
    count = 0

    def interrupted(messages, tier, json_mode):
        nonlocal count
        if "literary translator" in messages[0]["content"]:
            count += 1
            if count == 2:
                raise RuntimeError("simulated interruption")
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=interrupted)
    orch = Orchestrator(config, client=client)
    store = orch.prepare(str(source))
    with pytest.raises(RuntimeError, match="simulated interruption"):
        orch.run(str(source))
    partial = store.load_chapter(0)
    assert partial.text_segments[0].target == "译0"
    assert all(segment.target is None for segment in partial.text_segments[1:])
    first_call = next(call for call in client.calls if call["operation"] == "translation.body")
    assert _next_source(first_call["messages"][-1]["content"]) == sources[1]

    config.segment.max_tokens_per_batch = count_tokens(sources[0]) + count_tokens(sources[1])
    resumed = FakeClient(handler=routing_handler)
    Orchestrator(config, client=resumed).run(str(source))
    calls = [call for call in resumed.calls if call["operation"] == "translation.body"]
    assert [_numbered_sources(call["messages"][-1]["content"]) for call in calls] == [
        [sources[1]],
        sources[2:],
    ]
    assert [_next_source(call["messages"][-1]["content"]) for call in calls] == [sources[2], ""]
    assert "[Recent translations]\n译0" in calls[0]["messages"][-1]["content"]
    assert store.load_chapter(0).text_segments[0].target == "译0"
    assert all(segment.target for segment in store.load_chapter(0).text_segments)

    completed = FakeClient(handler=routing_handler)
    Orchestrator(config, client=completed).run(str(source))
    assert not [call for call in completed.calls if call["operation"] == "translation.body"]


@pytest.mark.parametrize("polish", [False, True])
def test_resume_after_target_save_completes_glossary_without_retranslation(
    tmp_path, config, monkeypatch, polish
):
    config.pipeline.polish = polish
    source = tmp_path / "book.txt"
    source.write_text("An unfinished sentence continues here.", encoding="utf-8")
    first_client = FakeClient(handler=routing_handler)
    first = Orchestrator(config, client=first_client)
    store = first.prepare(str(source))
    chapter = store.load_chapter(0)
    chapter.text_segments[0].target_before_polish = "stale pre-polish value"
    store.save_chapter(chapter)

    with monkeypatch.context() as patcher:

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt("before glossary checkpoint")

        patcher.setattr(first._translation, "extract_batch_glossary", interrupt)
        with pytest.raises(KeyboardInterrupt, match="before glossary checkpoint"):
            first.run(str(source))
    saved = store.load_chapter(0).text_segments[0]
    assert saved.target == ("润0" if polish else "译0")
    assert saved.target_before_polish == ("译0" if polish else None)
    assert not store.completed_batch_glossary_keys(0)

    client = FakeClient(handler=routing_handler)
    resumed = Orchestrator(config, client=client)
    resumed.run(str(source))
    store = resumed.run(str(source))
    assert store.load_chapter(0).text_segments[0] == saved
    assert store.completed_batch_glossary_keys(0)
    assert not [c for c in client.calls if c["operation"] in {"translation.body", "polish.body"}]


@pytest.mark.parametrize("checkpoint_saved", [False, True])
@pytest.mark.parametrize("polish", [False, True])
def test_resume_refreshes_full_glossary_before_pending_batches(
    tmp_path, config, monkeypatch, checkpoint_saved, polish
):
    """Keep saved targets and refresh the full glossary after recovering extraction."""
    config.pipeline.polish = polish
    source = tmp_path / "book.txt"
    source.write_text(
        "Alice waited by the window.\n\nThe door opened slowly.\n\nA letter arrived later.",
        encoding="utf-8",
    )

    def handler(messages, tier, json_mode):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "terminology" in system and "extractor" in system:
            terms = [{"source": "Alice", "target": "爱丽丝", "type": "person"}]
            return json.dumps({"terms": terms if "Alice" in user else []}, ensure_ascii=False)
        return routing_handler(messages, tier, json_mode)

    first = Orchestrator(config, client=FakeClient(handler=handler))
    store = first.prepare(str(source))
    # Append in a deliberately nonalphabetical order; neither source occurs in the book.
    store.upsert_term(GlossaryTerm(source="Zebra", target="斑马"))
    store.upsert_term(GlossaryTerm(source="Apple", target="苹果"))
    extract = first._translation.extract_batch_glossary

    def interrupt(*args, **kwargs):
        if checkpoint_saved:
            extract(*args, **kwargs)
        raise KeyboardInterrupt("after saving the first target")

    with monkeypatch.context() as patcher:
        patcher.setattr(first._translation, "extract_batch_glossary", interrupt)
        with pytest.raises(KeyboardInterrupt, match="first target"):
            first.run(str(source))
    saved = store.load_chapter(0).text_segments[0]
    assert saved.target is not None
    assert bool(store.completed_batch_glossary_keys(0)) == checkpoint_saved
    store.upsert_term(GlossaryTerm(source="Outside", target="外部术语"))

    client = FakeClient(handler=handler)
    resumed = Orchestrator(config, client=client)
    extracted_positions = []
    real_extract = resumed._translation.extract_batch_glossary

    def record_extraction(*args, **kwargs):
        extracted_positions.append(args[3])
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(resumed._translation, "extract_batch_glossary", record_extraction)
    resumed.run(str(source))
    assert extracted_positions == ([1, 2] if checkpoint_saved else [0, 1, 2])
    assert store.load_chapter(0).text_segments[0] == saved
    calls = [call for call in client.calls if call["operation"] == "translation.body"]
    assert len(calls) == 2
    for call in calls:
        user = call["messages"][-1]["content"]
        assert "Alice → 爱丽丝" in user
        assert "Outside → 外部术语" in user
        assert user.index("Zebra → 斑马") < user.index("Apple → 苹果")
        assert "Alice waited by the window." not in _numbered_sources(user)
    cached_client = FakeClient(handler=handler)
    Orchestrator(config, client=cached_client).run(str(source))
    assert cached_client.calls == []
