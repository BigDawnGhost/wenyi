"""Synthetic project directories for the catalog recovery and import suites.

The project builder lives here rather than in either collected suite so the two
can share one fixture without importing each other's test module.
"""

import hashlib

from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.sqlite import SqliteStorage


def orphan(workspace, pid="a" * 16, *, initialized=True, done=False):
    directory = workspace / "projects" / pid
    directory.mkdir(parents=True)
    source = directory / f"source-{'b' * 32}.txt"
    source.write_text("A short original.\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    storage = SqliteStorage(str(directory))
    try:
        if initialized:
            storage.init_from_document(
                Document(
                    title="Example",
                    source_path=str(source),
                    fmt="text",
                    source_lang="en",
                    target_lang="zh",
                    chapters=[
                        Chapter(
                            index=0,
                            title="Chapter",
                            segments=[Segment(index=0, source="A short original.")],
                        )
                    ],
                )
            )
            if done:
                chapter = storage.load_chapter(0)
                chapter.segments[0].target = "一小段原文。"
                storage.save_chapter_with_status(chapter, "done")
        else:
            storage.write_artifact(
                "preview.json", {"title": "Example", "fmt": "text", "source_lang": "en"}
            )
            storage.write_artifact("initializing.json", {"source_sha256": digest})
    finally:
        storage.close()
    return directory, source, digest
