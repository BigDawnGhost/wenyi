"""Read published precision candidates without changing checkpoints or chapter state."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .precision_archive import PrecisionArchive
from .protocol import Storage

Reason = Literal["no_archive", "incomplete", "source_mismatch", "ambiguous", "corrupt"]
CandidateId = Literal["T1", "T2", "T3"]


def _identity(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PrecisionCandidate:
    id: CandidateId
    target: str


@dataclass(frozen=True)
class PrecisionDrafts:
    chapter_index: int
    segment_index: int
    available: bool = False
    reason: Reason | None = "no_archive"
    candidates: list[PrecisionCandidate] = field(default_factory=list)
    synthesized_target: str | None = None
    before_polish_candidate: Literal["T1"] | None = None


class _Unavailable(Exception):
    reason: Reason

    def __init__(self, reason: Reason):
        self.reason = reason


def read_precision_drafts(store: Storage, ci: int, si: int) -> PrecisionDrafts:
    """Resolve stable segment identity in a short consistent read transaction.

    Missing/nontranslatable segments raise KeyError for transport-layer 404 handling.
    Published archives remain readable after human edits to the final translation.
    """
    with store.state_lock():
        chapter = store.load_chapter(ci)
        segments = chapter.text_segments
        positions = [i for i, segment in enumerate(segments) if segment.index == si]
        if len(positions) != 1 or segments[positions[0]].kind not in {"text", "heading"}:
            raise KeyError(si)
        position = positions[0]
        segment = segments[position]
        manifest = store.load_manifest()
        source_hash = manifest.get("source_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            return PrecisionDrafts(ci, si, reason="source_mismatch")
        archive = PrecisionArchive(store)
        matches: list[PrecisionDrafts] = []
        reasons: list[Reason] = []
        prefix = f"precision/chapters/{ci}/"
        for key in store.list_artifacts(prefix):
            if not key.endswith("/publication.json"):
                continue
            relative = key[len(prefix) :]
            match = re.fullmatch(r"(\d+)-(\d+)/([0-9a-f]{64})/publication\.json", relative)
            if match is None:
                continue
            start, count = int(match[1]), int(match[2])
            if not start <= position < start + count:
                continue
            root = key.rsplit("/", 1)[0]
            try:
                publication = store.read_artifact(key)
                if not isinstance(publication, dict):
                    raise _Unavailable("corrupt")
                if publication.get("status") not in {"ready", "published"}:
                    raise _Unavailable("incomplete")
                if publication.get("source_sha256") != source_hash:
                    raise _Unavailable("source_mismatch")
                if publication.get("fingerprint") != match[3]:
                    raise _Unavailable("corrupt")
                indices = publication.get("segment_indices")
                current_by_id = {item.index: item for item in segments}
                if (
                    not isinstance(indices, list)
                    or len(indices) != count
                    or len(set(indices)) != count
                    or any(
                        type(index) is not int or index not in current_by_id for index in indices
                    )
                ):
                    raise _Unavailable("source_mismatch")
                if si not in indices:
                    continue
                current = [current_by_id[index] for index in indices]
                offset = indices.index(si)

                fingerprint = match[3]

                def record(stage: str) -> dict[str, Any]:
                    value = store.read_artifact(f"{root}/{stage}.json")
                    if value is None:
                        raise _Unavailable("incomplete")
                    if (
                        not isinstance(value, dict)
                        or value.get("fingerprint") != fingerprint
                        or value.get("stage") != stage
                        or not isinstance(value.get("payload"), dict)
                        or value.get("payload_hash") != _identity(value["payload"])
                    ):
                        raise _Unavailable("corrupt")
                    return value["payload"]

                def targets(payload: dict[str, Any], name: str) -> list[str]:
                    value = (
                        archive.get(payload[f"{name}_ref"])
                        if f"{name}_ref" in payload
                        else payload.get(name)
                    )
                    if (
                        not isinstance(value, list)
                        or len(value) != count
                        or any(not isinstance(text, str) for text in value)
                    ):
                        raise _Unavailable("incomplete")
                    return value

                result_ref = publication.get("result_ref", f"{root}/ready.json")
                if result_ref not in {f"{root}/result.json", f"{root}/ready.json"}:
                    raise _Unavailable("corrupt")
                result = record("result" if result_ref.endswith("/result.json") else "ready")
                final, before = targets(result, "targets"), targets(result, "draft")
                if publication.get("target_hash") != _identity(final):
                    raise _Unavailable("corrupt")
                if "source_hashes" in publication:
                    source_matches = publication["source_hashes"] == [
                        _identity(item.source) for item in current
                    ]
                else:
                    inputs = store.read_artifact(f"{root}/inputs.json")
                    if not isinstance(inputs, dict) or not isinstance(inputs.get("plan"), dict):
                        raise _Unavailable("source_mismatch")
                    if _identity(inputs) != match[3]:
                        raise _Unavailable("corrupt")
                    source_matches = inputs["plan"].get("sources") == [
                        item.source for item in current
                    ]
                if not source_matches:
                    raise _Unavailable("source_mismatch")
                drafts = [
                    targets(record(f"drafts/{name}"), "targets") for name in ("T1", "T2", "T3")
                ]
                if drafts[0] != before:
                    raise _Unavailable("corrupt")
                if [item.target_before_polish for item in current] != before:
                    raise _Unavailable("incomplete")
                if any(item.target is None for item in current) or (
                    publication["status"] == "ready" and [item.target for item in current] != final
                ):
                    raise _Unavailable("incomplete")
                matches.append(
                    PrecisionDrafts(
                        ci,
                        si,
                        True,
                        None,
                        [
                            PrecisionCandidate(name, draft[offset])
                            for name, draft in zip(("T1", "T2", "T3"), drafts)
                        ],
                        final[offset],
                        "T1",
                    )
                )
            except _Unavailable as error:
                reasons.append(error.reason)
            except (ValueError, TypeError, KeyError, OSError):
                reasons.append("corrupt")
        exact = [item for item in matches if item.synthesized_target == segment.target]
        preferred = exact or matches
        unique = {
            (tuple((c.id, c.target) for c in item.candidates), item.synthesized_target): item
            for item in preferred
        }
        if len(unique) == 1:
            return next(iter(unique.values()))
        if unique:
            return PrecisionDrafts(ci, si, reason="ambiguous")
        for reason in ("corrupt", "source_mismatch", "incomplete"):
            if reason in reasons:
                return PrecisionDrafts(ci, si, reason=reason)
        return PrecisionDrafts(ci, si)
