"""Small book and fake-model configuration builders for review tests."""

import json
import re
from pathlib import Path

from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.pipeline.runstore import STATUS_DONE
from wenyi_core.review.evidence import BookEvidenceIndex
from wenyi_core.storage.file import FileStorage


def _config() -> Config:
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {
                    "default_strong": {"provider": "default", "model": "strong"},
                    "default_cheap": {"provider": "default", "model": "cheap"},
                },
            },
            "pipeline": {
                "review_agent_max_evidence_rounds": 2,
            },
        }
    )


def _chapter(index: int, texts: list[tuple[str, str]]) -> Chapter:
    return Chapter(
        index=index,
        title=f"Chapter {index}",
        segments=[
            Segment(index=segment_index, source=source, target=target)
            for segment_index, (source, target) in enumerate(texts)
        ],
        meta={"source_digest": f"Digest {index}"},
    )


def review_evidence() -> BookEvidenceIndex:
    return BookEvidenceIndex(
        [
            _chapter(
                0,
                [
                    ("Ann arrived.", "安到了。"),
                    ("Ann spoke.", "安开口了。"),
                ],
            )
        ],
        [GlossaryTerm(source="Ann", target="安", type="person")],
        {},
    )


def autofix_config(state_dir: str) -> Config:
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {"default_strong": {"provider": "default", "model": "p"}},
            },
            "pipeline": {
                "review_autofix": True,
                "review_concurrency": 1,
            },
            "output": {"punctuation_normalize": False},
            "paths": {"state_dir": state_dir},
        }
    )


def autofix_store(directory: str, target: str = "正式译文。") -> FileStorage:
    store = FileStorage(str(Path(directory, "state", "book")))
    store.save_chapter(
        Chapter(
            index=0,
            title="第一章",
            segments=[Segment(index=0, source="原文。", target=target)],
        )
    )
    store.save_manifest(
        {
            "title": "book",
            "source_lang": "ja",
            "target_lang": "zh",
            "source_sha256": "0" * 64,
            "chapters": [{"index": 0, "status": STATUS_DONE}],
        }
    )
    return store


def review_json(user: str, issues: list[dict]) -> str:
    """Build a reviewer response with its completeness receipt."""
    return json.dumps(
        {
            "issues": issues,
            "reviewed_segments": len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE)),
            "complete": True,
        },
        ensure_ascii=False,
    )


def fix_json(user: str, replacement: str) -> str:
    """Echo identity fields from a fixer request into a complete temporary replacement."""

    def field(name: str) -> str:
        match = re.search(rf"^{name}:\s*(.+)$", user, re.MULTILINE)
        if match is None:
            raise AssertionError(f"Fixer prompt missing {name}")
        return match.group(1).strip()

    return json.dumps(
        {
            "segment_ref": field("segment_ref"),
            "before_hash": field("before_hash"),
            "issue_ids": json.loads(field("issue_ids")),
            "replacement": replacement,
            "complete": True,
        },
        ensure_ascii=False,
    )
