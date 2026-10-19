"""Text handlers operate on complete paragraphs without changing stable identities."""

import hashlib

import pytest
from wenyi_core.assemble.export_view import ExportViewStore
from wenyi_core.i18n.policy.models import ExportTextInput, ExportTextResult
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.pipeline.runstore import ExportSnapshotStore
from wenyi_core.postprocess.export_text import boundary_map, normalize_chinese, validate_result


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


@pytest.mark.parametrize("mark", ["—", "…"])
def test_boundary_map_includes_punctuation_inserted_at_paragraph_start(mark):
    before = mark + "你好"
    after = mark * 2 + "你好"
    assert boundary_map(before, after) == (0, 2, 3, 4)


@pytest.mark.parametrize("mark", ["—", "…"])
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("empty_segments", [False, True])
def test_normalization_preserves_leading_punctuation_and_segment_boundaries(
    mark, split, empty_segments
):
    targets = (mark, "你好") if split else (mark + "你好",)
    if empty_segments:
        targets = ("", *targets, "")
    request = ExportTextInput(
        tuple(3 + index * 4 for index in range(len(targets))),
        targets,
        (False, *(True for _ in targets[1:])),
        ((0, len(targets)),),
    )
    result = normalize_chinese(request)
    expected = (mark * 2, "你好") if split else (mark * 2 + "你好",)
    if empty_segments:
        expected = ("", *expected, "")
    assert result.targets == expected
    assert result.segment_ids == request.segment_ids
    assert result.continuations == request.continuations
    assert result.logical_ranges == request.logical_ranges
    assert result.logical_boundary_maps == ((0, 2, 3, 4),)


def test_validation_rejects_a_boundary_map_that_omits_the_paragraph_prefix():
    request = ExportTextInput((0,), ("甲",), (False,), ((0, 1),))
    result = ExportTextResult((0,), ("乙甲",), (False,), ((0, 1),), ((1, 2),))
    with pytest.raises(ValueError, match="Invalid export text boundary map"):
        validate_result(request, result)


@pytest.mark.parametrize("mark", ["—", "…"])
def test_leading_punctuation_remaps_annotations_and_styles_without_changing_snapshot(
    tmp_path, mark
):
    before = mark + "你好"
    after = mark * 2 + "你好"
    metadata = {
        "target_digest": hashlib.sha256(before.encode()).hexdigest(),
        "placements": [{"id": "span", "target_start": 0, "target_end": 1}],
    }
    chapter = Chapter(
        index=0,
        segments=[
            Segment(
                index=3,
                source="First source fragment.",
                target=mark,
                meta={"epub_annotations": metadata, "docx_styles": metadata},
            ),
            Segment(index=7, source="Second source fragment.", target="你好", cont=True),
        ],
    )
    snapshot = ExportSnapshotStore(
        str(tmp_path), {"source_lang": "en", "target_lang": "zh"}, {0: chapter}
    )
    for _ in range(2):
        exported = ExportViewStore(snapshot, punctuation_normalize=True).load_chapter(0)
        assert [segment.target for segment in exported.text_segments] == [mark * 2, "你好"]
        for key in ("epub_annotations", "docx_styles"):
            remapped = exported.text_segments[0].meta[key]
            assert remapped["target_digest"] == hashlib.sha256(after.encode()).hexdigest()
            assert remapped["placements"] == [{"id": "span", "target_start": 0, "target_end": 2}]
        assert snapshot.load_chapter(0) == chapter
