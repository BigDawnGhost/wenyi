"""Synthetic book and file storage for glossary workflow tests."""

from wenyi_core.config import Config
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.file import FileStorage


def book_store(tmp_path):
    chapters = [
        Chapter(index=0, segments=[Segment(index=7, source="Alice waited for Carl, her uncle.")]),
        Chapter(
            index=1,
            segments=[
                Segment(index=3, source="Beth was Alice's mother."),
                Segment(index=9, source="Carl was Beth's elder brother."),
            ],
        ),
    ]
    store = FileStorage(str(tmp_path / "run"))
    source = tmp_path / "source.txt"
    source.write_text("\n".join(s.source for chapter in chapters for s in chapter.segments))
    store.init_from_document(
        Document(
            title="Invented family",
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=chapters,
        )
    )
    return store, chapters


def inputs(tmp_path):
    source = tmp_path / "family.txt"
    source.write_text(
        "# First\n\nAlice waited for Carl, her uncle.\n\n"
        "# Later\n\nBeth was Alice's mother.\n\nCarl was Beth's elder brother.\n",
        encoding="utf-8",
    )
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "book_understanding": False,
                "review": False,
                "polish": False,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    return source, config, FileStorage(str(tmp_path / "run"))
