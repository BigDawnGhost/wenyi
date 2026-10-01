"""Pure text operations over detached logical paragraphs and validated boundary maps."""

from __future__ import annotations

from difflib import SequenceMatcher

from ..i18n.policy.models import ExportTextInput, ExportTextResult
from .punct import normalize_zh_segments


def boundary_map(before: str, after: str) -> tuple[int, ...]:
    """Map boundaries with the existing tested punctuation diff affinity."""
    mapping = [0] * (len(before) + 1)
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    for operation, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        before_length = before_end - before_start
        after_length = after_end - after_start
        if operation == "equal":
            for offset in range(before_length + 1):
                mapping[before_start + offset] = after_start + offset
        elif operation == "insert":
            mapping[before_start] = after_end
        else:
            for offset in range(before_length + 1):
                mapped_offset = (offset * after_length + before_length // 2) // before_length
                mapping[before_start + offset] = after_start + mapped_offset
    return tuple(mapping)


def validate_result(request: ExportTextInput, result: ExportTextResult) -> None:
    """Reject identity, completion or invalid offset changes before applying any result."""
    if (
        request.segment_ids != result.segment_ids
        or request.continuations != result.continuations
        or request.logical_ranges != result.logical_ranges
        or len(request.targets) != len(result.targets)
    ):
        raise ValueError("Export text operation changed stable segment identities")
    for before, after in zip(request.targets, result.targets):
        if (before is None) != (after is None) or before == "" and after != "":
            raise ValueError("Export text operation changed a pending or empty target")
    if len(result.logical_boundary_maps) != len(request.logical_ranges):
        raise ValueError("Export text operation omitted paragraph boundary maps")
    cursor = 0
    for (start, end), mapping in zip(request.logical_ranges, result.logical_boundary_maps):
        if start != cursor or not start < end <= len(request.targets):
            raise ValueError("Invalid logical paragraph ranges")
        cursor = end
        before = "".join(text or "" for text in request.targets[start:end])
        after = "".join(text or "" for text in result.targets[start:end])
        if (
            len(mapping) != len(before) + 1
            or any(type(value) is not int for value in mapping)
            or tuple(sorted(mapping)) != mapping
            or any(value < 0 or value > len(after) for value in mapping)
            or mapping[-1] != len(after)
        ):
            raise ValueError("Invalid export text boundary map")
    if cursor != len(request.targets):
        raise ValueError("Logical paragraph ranges do not cover the segments")


def normalize_chinese(request: ExportTextInput) -> ExportTextResult:
    """Normalize a paragraph before splitting at mapped original segment boundaries."""
    targets = list(request.targets)
    maps = []
    for start, end in request.logical_ranges:
        values = request.targets[start:end]
        before = "".join(text or "" for text in values)
        # A partially translated paragraph lacks the context for a safe full transformation.
        after = before if None in values else normalize_zh_segments([before])[0]
        mapping = boundary_map(before, after)
        maps.append(mapping)
        position = 0
        for index in range(start, end):
            value = request.targets[index]
            if value is not None:
                next_position = position + len(value)
                targets[index] = after[mapping[position] : mapping[next_position]] if value else ""
                position = next_position
    result = ExportTextResult(
        request.segment_ids,
        tuple(targets),
        request.continuations,
        request.logical_ranges,
        tuple(maps),
    )
    validate_result(request, result)
    return result
