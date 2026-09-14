"""以纯函数方式核对旧版 EPUB 注释标记文本槽位。"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from trans_novel.epub.slots import (
    EpubTextSlot,
    normalized_source_text,
    normalized_target_text,
    slot_contract_digest,
)
from trans_novel.ingest.models import Chapter, Document, Segment, chapter_source_digest

_PREFIX = "EPUB note migration rejected:"
_WS = re.compile(r"[ \t\r\n\f\v]+")
_NUMBER = re.compile(r"(?:([0-9]{1,4})|\[([0-9]{1,4})\]|\(([0-9]{1,4})\))")


@dataclass(frozen=True)
class _Marker:
    resource: str
    path: tuple[int, ...]
    label: str


def _reject(detail: str) -> ValueError:
    return ValueError(f"{_PREFIX} {detail}")


def _markers(doc: Document) -> dict[str, tuple[_Marker, ...]]:
    raw = doc.meta.get("epub_notes")
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise _reject("current source has no supported note relation data")
    items = raw.get("markers")
    if not isinstance(items, list):
        raise _reject("current source note relation data is invalid")
    by_resource: dict[str, list[_Marker]] = defaultdict(list)
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for item in items:
        if not isinstance(item, dict):
            raise _reject("current source note relation data is invalid")
        resource, path, label = item.get("resource_href"), item.get("path"), item.get("label")
        if (
            not isinstance(resource, str)
            or not isinstance(path, list)
            or not path
            or any(type(part) is not int or part < 0 for part in path)
            or not isinstance(label, str)
            or not label
        ):
            raise _reject("current source note relation data is invalid")
        key = (resource, tuple(path))
        if key in seen:
            raise _reject(f"duplicate marker relation at {resource}:{path}")
        seen.add(key)
        by_resource[resource].append(_Marker(resource, tuple(path), label))
    return {key: tuple(value) for key, value in by_resource.items()}


def _owned_marker(
    resource: str,
    block_path: tuple[int, ...],
    slot: EpubTextSlot,
    markers: dict[str, tuple[_Marker, ...]],
) -> _Marker | None:
    location = (*block_path, *slot.element_path)
    owners = [
        marker
        for marker in markers.get(resource, ())
        if (location == marker.path and slot.field == "text")
        or (len(location) > len(marker.path) and location[: len(marker.path)] == marker.path)
    ]
    if len(owners) > 1:
        raise _reject(f"ambiguous marker ownership at {resource}:{location}:{slot.field}")
    return owners[0] if owners else None


def _normalized(value: str) -> str:
    return _WS.sub(" ", value).strip()


def _is_marker_token(value: str, label: str) -> bool:
    value, label = _normalized(value), _normalized(label)
    if value == label:
        return True
    value_number, label_number = _NUMBER.fullmatch(value), _NUMBER.fullmatch(label)
    if value_number is None or label_number is None:
        return False
    value_digits = next(group for group in value_number.groups() if group is not None)
    label_digits = next(group for group in label_number.groups() if group is not None)
    return value_digits == label_digits


def _validate_state(segment: Segment, locator: str) -> None:
    state = segment.epub_state
    if state is None:
        raise _reject(f"missing EPUB slot state at {locator}")
    if state.slot_contract_sha256 != slot_contract_digest(state.slots):
        raise _reject(f"stale slot contract at {locator}")
    if segment.source != normalized_source_text(state.slots):
        raise _reject(f"stale segment source at {locator}")
    assigned = [slot.target_value is not None for slot in state.slots]
    if any(assigned) and not all(assigned):
        raise _reject(f"partially assigned slot run at {locator}")
    expected = normalized_target_text(state.slots) if assigned and all(assigned) else None
    if segment.target != expected:
        raise _reject(f"stale segment target derivation at {locator}")


def _same_source_block(current: Segment, old: Segment) -> bool:
    fresh, persisted = current.epub_state, old.epub_state
    assert fresh is not None and persisted is not None
    return (
        current.anchor == old.anchor
        and current.resource_href == old.resource_href
        and current.kind == old.kind
        and fresh.resource_href == persisted.resource_href
        and fresh.resource_sha256 == persisted.resource_sha256
        and fresh.block_path == persisted.block_path
        and fresh.block_fingerprint == persisted.block_fingerprint
        and fresh.parse_mode == persisted.parse_mode
    )


def _relocate_segment(
    current: Segment,
    old: Segment,
    markers: dict[str, tuple[_Marker, ...]],
    locator: str,
) -> Segment:
    _validate_state(current, locator)
    _validate_state(old, locator)
    if not _same_source_block(current, old):
        raise _reject(f"stale source block at {locator}")
    fresh_state, old_state = current.epub_state, old.epub_state
    assert fresh_state is not None and old_state is not None
    old_by_location = {(slot.element_path, slot.field): slot for slot in old_state.slots}
    fresh_locations = [(slot.element_path, slot.field) for slot in fresh_state.slots]
    if any(location not in old_by_location for location in fresh_locations):
        raise _reject(f"new or changed source slot at {locator}")
    slots = [
        slot.model_copy(update={"target_value": old_by_location[location].target_value})
        for slot, location in zip(fresh_state.slots, fresh_locations, strict=True)
    ]
    slots_by_location = dict(zip(fresh_locations, slots, strict=True))
    surviving = set(fresh_locations)
    removed: list[tuple[int, EpubTextSlot, _Marker]] = []
    for position, slot in enumerate(old_state.slots):
        location = (slot.element_path, slot.field)
        if location in surviving:
            if slot.source_value != slots_by_location[location].source_value:
                raise _reject(f"changed source slot at {locator}")
            continue
        marker = _owned_marker(old_state.resource_href, old_state.block_path, slot, markers)
        if marker is None:
            raise _reject(f"unsupported missing slot at {locator}:{slot.element_path}:{slot.field}")
        removed.append((position, slot, marker))

    grouped: dict[_Marker, list[tuple[int, EpubTextSlot]]] = defaultdict(list)
    for position, slot, marker in removed:
        grouped[marker].append((position, slot))
    old_positions = {
        (slot.element_path, slot.field): pos for pos, slot in enumerate(old_state.slots)
    }
    additions_before: dict[int, str] = defaultdict(str)
    additions_after: dict[int, str] = defaultdict(str)
    for marker, owned in sorted(grouped.items(), key=lambda item: item[1][0][0]):
        if not _is_marker_token("".join(slot.source_value for _, slot in owned), marker.label):
            raise _reject(f"unknown marker source data at {marker.resource}:{marker.path}")
        targets = [slot.target_value for _, slot in owned]
        if all(value is None for value in targets):
            continue
        if any(value is None for value in targets):
            raise _reject(f"ambiguous marker target attribution at {marker.resource}:{marker.path}")
        payload = "".join(value or "" for value in targets)
        if _is_marker_token(payload, marker.label):
            continue
        start, end = owned[0][0], owned[-1][0]
        following = [
            i
            for i, slot in enumerate(slots)
            if slot.source_value.strip() and old_positions[fresh_locations[i]] > end
        ]
        if following:
            additions_before[following[0]] += payload
            continue
        preceding = [
            i
            for i, slot in enumerate(slots)
            if slot.source_value.strip() and old_positions[fresh_locations[i]] < start
        ]
        if not preceding:
            raise _reject(f"no body slot for marker payload at {marker.resource}:{marker.path}")
        additions_after[preceding[-1]] += payload
    slots = [
        slot.model_copy(
            update={
                "target_value": (
                    additions_before[index] + (slot.target_value or "") + additions_after[index]
                    if slot.target_value is not None
                    else None
                )
            }
        )
        for index, slot in enumerate(slots)
    ]
    state = fresh_state.model_copy(
        update={"slots": slots, "slot_contract_sha256": slot_contract_digest(slots)}
    )
    target = (
        normalized_target_text(slots) if all(s.target_value is not None for s in slots) else None
    )
    migrated = current.model_copy(
        update={
            "index": old.index,
            "target": target,
            "epub_state": state,
            "preserve_source": old.preserve_source,
        }
    )
    return Segment.model_validate(migrated.model_dump(mode="python"))


def _has_marker_only_segment(
    chapters: list[Chapter], markers: dict[str, tuple[_Marker, ...]]
) -> bool:
    for chapter in chapters:
        for segment in chapter.segments:
            state = segment.epub_state
            if (
                state is not None
                and state.slots
                and all(
                    _owned_marker(state.resource_href, state.block_path, slot, markers) is not None
                    for slot in state.slots
                )
            ):
                return True
    return False


def reconcile_note_slots(current: Document, persisted: list[Chapter]) -> list[Chapter]:
    """返回符合当前 schema 的章节副本，并保留误放入旧标记槽位的已付费正文译文。"""
    if current.fmt != "epub":
        raise _reject("current document is not EPUB")
    markers = _markers(current)
    if len(current.chapters) != len(persisted):
        if _has_marker_only_segment(persisted, markers):
            raise _reject("marker-only segment requires structural migration")
        raise _reject("chapter grouping changed")
    result: list[Chapter] = []
    for fresh_chapter, old_chapter in zip(current.chapters, persisted, strict=True):
        if fresh_chapter.href != old_chapter.href:
            raise _reject(f"chapter grouping changed at chapter {old_chapter.index}")
        if len(fresh_chapter.segments) != len(old_chapter.segments):
            if _has_marker_only_segment([old_chapter], markers):
                raise _reject(
                    "marker-only segment requires structural migration "
                    f"at chapter {old_chapter.index}"
                )
            raise _reject(f"chapter grouping changed at chapter {old_chapter.index}")
        segments = [
            _relocate_segment(
                fresh, old, markers, f"chapter {old_chapter.index} segment {old.index}"
            )
            for fresh, old in zip(fresh_chapter.segments, old_chapter.segments, strict=True)
        ]
        chapter = fresh_chapter.model_copy(
            update={
                "index": old_chapter.index,
                "segments": segments,
                "processing": old_chapter.processing,
            }
        )
        if chapter.processing is not None:
            chapter.processing = chapter.processing.model_copy(
                update={"source_sha256": chapter_source_digest(chapter)}
            )
        result.append(chapter)
    return [Chapter.model_validate(chapter.model_dump(mode="python")) for chapter in result]


__all__ = ["reconcile_note_slots"]
