"""Bounded chapter windows and conditional paragraph translation refreshes."""

import hashlib
import json
from typing import cast

from wenyi_core.config import Config
from wenyi_core.pipeline.prepare.input_preparation import ingest_config
from wenyi_core.storage.chapter_reading import ChapterReader
from wenyi_core.storage.protocol import Storage


def translation_revision(segment: dict) -> str:
    """Content identity, not a timestamp: null and a saved empty target are distinct."""
    value = [segment["index"], segment.get("target"), segment.get("target_before_polish")]
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def parsed_identity(project: dict, config: Config) -> dict | None:
    if project.get("initialized"):
        return None
    return {
        "source_sha256": project.get("source_sha256"),
        "ingest_config": ingest_config(config),
    }


def window(
    storage: Storage,
    ci: int,
    *,
    offset: int,
    limit: int,
    around: int | None,
    parsed: dict | None,
) -> dict:
    result = cast(ChapterReader, storage).read_chapter_window(
        ci, offset=offset, limit=limit, around=around, parsed=parsed
    )
    for segment in result["segments"]:
        segment["revision"] = translation_revision(segment)
    return result


def updates(storage: Storage, ci: int, known: dict[int, str]) -> list[dict]:
    """Only changed translations cross the wire; original text is never resent."""
    changed = []
    for segment in cast(ChapterReader, storage).read_chapter_targets(ci, list(known)):
        revision = translation_revision(segment)
        previous = known[segment["index"]]
        if revision != previous:
            changed.append({**segment, "revision": revision, "previous_revision": previous})
    return changed
