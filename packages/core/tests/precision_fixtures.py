"""Shared fixed-book fixtures and fake responses for precision tests."""

import json
from collections import Counter
from pathlib import Path
from threading import Lock

from wenyi_core.config import Config
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.pipeline.runstore import STATUS_PENDING
from wenyi_core.pipeline.translation_batch import BatchPlan
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler


def precision_config(tmp_path: Path) -> Config:
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "translation_mode": "best_of_three",
                "review": False,
                "book_understanding": False,
                "annotation_alignment": False,
            },
            "paths": {"state_dir": str(tmp_path)},
        }
    )


def precision_store(tmp_path: Path, sources: tuple[str, ...] = ("one", "two")) -> FileStorage:
    store = FileStorage(str(tmp_path / "book"))
    store.save_chapter(
        Chapter(
            index=0,
            segments=[Segment(index=i + 10, source=source) for i, source in enumerate(sources)],
        )
    )
    store.save_manifest(
        {
            "title": "book",
            "source_lang": "en",
            "target_lang": "zh",
            "source_sha256": "0" * 64,
            "chapters": [{"index": 0, "status": STATUS_PENDING}],
        }
    )
    return store


def artifact(store: FileStorage, key: str) -> dict:
    value = store.read_artifact(key)
    assert isinstance(value, dict), f"Expected a JSON object at {key}"
    return value


def precision_plan(store: FileStorage, allow_empty: bool = False) -> BatchPlan:
    segments = store.load_chapter(0).text_segments
    return BatchPlan.capture(
        0,
        0,
        segments,
        [],
        "prior",
        "style",
        "synopsis",
        "digest",
        [[] for _ in segments],
        "following",
        allow_empty_translations=allow_empty,
    )


class PrecisionHandler:
    def __init__(self, invalid=False, blank=False):
        self.counts, self.lock = Counter(), Lock()
        self.invalid, self.blank = invalid, blank

    def __call__(self, messages, tier, json_mode):
        if "Task (JSON):\n" not in messages[-1]["content"]:
            return routing_handler(messages, tier, json_mode)
        sources = json.loads(messages[1]["content"].split("\n", 1)[1])["sources"]
        task = json.loads(messages[-1]["content"].split("Task (JSON):\n")[1])
        kind = "synthesis" if "drafts" in task else "translate"
        with self.lock:
            self.counts[kind] += 1
            sample = self.counts[kind]
        if kind == "synthesis" and self.invalid:
            return '{"translations":[]}'
        return json.dumps(
            {
                "translations": [
                    source
                    if not any(character.isalpha() for character in source)
                    else ""
                    if self.blank
                    else f"{'润' if kind == 'synthesis' else '译'}{sample}:{i}"
                    for i, source in enumerate(sources)
                ]
            }
        )
