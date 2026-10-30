"""Reusable API test data builders; fixtures live in conftest."""

from wenyi_api import dal
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.pipeline.runstore import source_sha256


def new_project(api, source="en", target="zh"):
    # Seed existing projects for endpoint tests; public creation requires a source.
    return dal.create_project("Book", source, target, {"template": "标准翻译"})


def document(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Book\nOriginal paragraph", encoding="utf-8")
    return Document(
        title="Book",
        fmt="text",
        source_lang="en",
        target_lang="zh",
        source_path=str(source),
        meta={
            "format_metadata": {"nested": [1, 2]},
            "epub_annotation_contexts": {"note": {"source": "Original note"}},
        },
        chapters=[
            Chapter(
                index=0,
                title="Chapter",
                href="chapter.xhtml",
                template="<p id='a'></p>",
                meta={"toc_entry_id": "toc-a"},
                segments=[
                    Segment(
                        index=0,
                        source="Original paragraph",
                        target="润色译文",
                        target_before_polish="原始译文",
                        anchor="a",
                        resource_href="chapter.xhtml",
                        meta={"style": {"bold": True}},
                    )
                ],
            )
        ],
    )


def initialize(storage, tmp_path):
    doc = document(tmp_path)
    digest = source_sha256(doc.source_path)
    storage.begin_initialization(digest)
    manifest = storage.stage_document(doc, source_hash=digest)
    storage.save_manifest(manifest)
    storage.finish_initialization()
    return doc, digest
