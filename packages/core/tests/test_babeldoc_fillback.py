"""BabelDOC fillback must retain IDs whose saved translation is blank."""

import pytest
from wenyi_core.ingest.pdf_babeldoc import translations_from_store
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.mark.parametrize("storage_type", [FileStorage, SqliteStorage])
@pytest.mark.parametrize("target", [None, "", " \t\n", "\u3000", " . "])
@pytest.mark.parametrize("punctuation_only", [False, True])
def test_blank_targets_keep_fillback_ids_without_changing_state(
    tmp_path, storage_type, target, punctuation_only
):
    segments = [Segment(index=35, source=".", target=target, meta={"babeldoc_id": "2:4"})]
    if not punctuation_only:
        segments += [
            Segment(index=36, source="Body.", target="正文。", meta={"babeldoc_id": "2:5"}),
            Segment(index=37, source="No bridge ID", target="忽略"),
        ]
    document = Document(
        title="Synthetic PDF",
        source_lang="en",
        target_lang="zh",
        source_path="synthetic.pdf",
        fmt="pdf",
        meta={
            "babeldoc": True,
            "babeldoc_session_id": "offline-session",
            "babeldoc_bridge_url": "http://bridge.invalid",
        },
        chapters=[Chapter(index=0, segments=segments)],
    )
    store = storage_type(str(tmp_path / "run"))
    try:
        manifest = store.stage_document(document, source_hash="0" * 64)
        store.save_manifest(manifest)
        before = store.load_chapter(0).model_dump()

        session, url, translations = translations_from_store(store)

        expected = {"2:4": target if target and target.strip() else "."}
        if not punctuation_only:
            expected["2:5"] = "正文。"
        assert (session, url) == ("offline-session", "http://bridge.invalid")
        assert translations == expected
        assert store.load_chapter(0).model_dump() == before
    finally:
        store.close()
