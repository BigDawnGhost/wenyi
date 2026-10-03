"""Text handlers operate on complete paragraphs without changing stable identities."""

from wenyi_core.i18n.policy.models import ExportTextInput
from wenyi_core.postprocess.export_text import normalize_chinese


def test_export_identity_binds_targets_and_output_choices_not_cache_timestamps(tmp_path):
    from wenyi_core.assemble.policy import export_plan
    from wenyi_core.ingest.models import Chapter, Document, Segment
    from wenyi_core.storage.file import FileStorage

    source = tmp_path / "source.txt"
    source.write_text("Original.")
    chapter = Chapter(index=0, segments=[Segment(index=0, source="Original.", target=None)])
    store = FileStorage(str(tmp_path / "run"))
    store.init_from_document(
        Document(
            title="Test",
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=[chapter],
        )
    )
    initial = export_plan(store, "epub")
    manifest = store.load_manifest()
    manifest["created_at"] = "changed timestamp"
    store.save_manifest(manifest)
    chapter.meta["source_digest_policy"] = "irrelevant analysis identity"
    store.save_chapter(chapter)
    assert export_plan(store, "epub").fingerprint == initial.fingerprint
    assert export_plan(store, "epub", bilingual=True).fingerprint != initial.fingerprint
    assert export_plan(store, "epub", about_page=False).fingerprint != initial.fingerprint
    chapter.segments[0].target = ""
    store.save_chapter(chapter)
    assert export_plan(store, "epub").fingerprint != initial.fingerprint


def test_handler_failure_never_replaces_an_output_or_formal_targets(tmp_path, monkeypatch):
    from types import MappingProxyType

    import pytest
    from wenyi_core.assemble.writer import assemble
    from wenyi_core.ingest.models import Chapter, Document, Segment
    from wenyi_core.storage.file import FileStorage

    source = tmp_path / "source.txt"
    source.write_text("Original.")
    chapters = [
        Chapter(index=index, segments=[Segment(index=0, source="Original.", target="中文.")])
        for index in (0, 1)
    ]
    document = Document(
        title="Test",
        source_lang="en",
        target_lang="zh",
        fmt="text",
        source_path=str(source),
        chapters=chapters,
    )
    store = FileStorage(str(tmp_path / "run"))
    store.init_from_document(document)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            raise ValueError("handler failed")
        return normalize_chinese(request)

    monkeypatch.setattr(
        "wenyi_core.assemble.export_view.TEXT_HANDLERS",
        MappingProxyType({"punctuation.zh_cn": handler}),
    )
    output = tmp_path / "output.txt"
    output.write_text("Existing output.")
    with pytest.raises(ValueError, match="handler failed"):
        assemble(store, str(source), str(output), "txt", punctuation_normalize=True)
    assert output.read_text() == "Existing output."
    assert [store.load_chapter(index) for index in (0, 1)] == chapters


def test_split_ellipsis_is_processed_as_one_logical_paragraph():
    request = ExportTextInput((3, 7), ('他说"等等.', '..然后走了"'), (False, True), ((0, 2),))
    result = normalize_chinese(request)
    assert "".join(text or "" for text in result.targets) == "他说“等等……然后走了”"
    assert result.segment_ids == request.segment_ids
    assert result.continuations == request.continuations
    assert result.logical_ranges == request.logical_ranges
    mapping = result.logical_boundary_maps[0]
    assert tuple(sorted(mapping)) == mapping
    assert len(mapping) == len("".join(text or "" for text in request.targets)) + 1
    assert mapping[-1] == len("".join(text or "" for text in result.targets))
    assert (
        normalize_chinese(
            ExportTextInput(
                result.segment_ids, result.targets, result.continuations, result.logical_ranges
            )
        ).targets
        == result.targets
    )


def test_pending_and_intentionally_empty_targets_remain_distinct():
    request = ExportTextInput(
        (0, 1, 2), (None, "", "中文."), (False, False, False), ((0, 1), (1, 2), (2, 3))
    )
    assert normalize_chinese(request).targets == (None, "", "中文。")
