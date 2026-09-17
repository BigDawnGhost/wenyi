"""Project review evidence and publication records into read-only workbench rows."""

from __future__ import annotations

import re
from typing import Any

from wenyi_core.review.autofix_models import integer_index


def _rows(value) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _keys(item: dict) -> set[str]:
    values = [item.get("issue_key"), item.get("issue_id")]
    for key in ("issue_keys", "issue_ids"):
        if isinstance(item.get(key), list):
            values.extend(item[key])
    return {value for value in values if isinstance(value, str) and value}


def _status(records: list[dict]) -> str:
    statuses = [record.get("status") for record in records]
    if any(status in {"failed", "not_applied"} for status in statuses):
        return "failed"
    if statuses and all(status == "applied" for status in statuses):
        return "fixed"
    if statuses and all(status == "not_applied_no_net_change" for status in statuses):
        return "unchanged"
    return "pending"


def review_items(storage, rid: str, result: dict, autofix: dict) -> list[dict[str, Any]]:
    """Join by issue identity, retaining unlinked suggestions and publication failures.

    Source/current-target excerpts are explicitly current chapter data, not invented
    historical evidence. Paragraph links use Segment.index, never a review text offset.
    """
    detailed = _rows(storage.read_artifact(f"reviews/{rid}/rounds/final/unresolved_issues.json"))
    details = {key: row for row in detailed for key in _keys(row)}
    changes = _rows(result.get("changes"))
    index = autofix.get("index")
    locations = _rows(index.get("locations")) if isinstance(index, dict) else []
    publications = {
        record_id: row
        for row in locations
        for record_id in row.get("record_ids", [])
        if isinstance(record_id, str)
    }
    records = []
    for record in _rows(autofix.get("records")):
        published = publications.get(record.get("record_id"))
        if (
            published is not None
            and published.get("chapter") == record.get("chapter")
            and published.get("index") == record.get("index")
        ):
            record = {**record, "publication": published}
        records.append(record)
    used_changes: set[int] = set()
    used_records: set[int] = set()
    chapters = {}

    def location(chapter_index, text_index, expected_segment=None):
        ci, ti = integer_index(chapter_index), integer_index(text_index)
        if ci is None or ti is None or ci < 0 or ti < 0:
            return None
        if ci not in chapters:
            try:
                chapter = storage.load_chapter(ci)
                chapters[ci] = (
                    (chapter.meta.get("title_translated") or chapter.title, chapter.text_segments)
                    if chapter is not None
                    else None
                )
            except (KeyError, FileNotFoundError):
                chapters[ci] = None
        data = chapters[ci]
        if data is None or ti >= len(data[1]):
            return None
        title, segments = data
        segment = segments[ti]
        if expected_segment is not None and segment.index != expected_segment:
            return None
        return {
            "chapter": ci,
            "text_index": ti,
            "segment_index": segment.index,
            "chapter_title": title,
            "source": segment.source,
            "current_target": segment.target,
        }

    def item(key, kind, value, suggestions, publications):
        refs = []
        for record in [value, *publications]:
            if isinstance(record.get("evidence_refs"), list):
                refs.extend(record["evidence_refs"])
        evidence = []
        for ref in dict.fromkeys(ref for ref in refs if isinstance(ref, str)):
            match = re.fullmatch(r"ch(\d+):text(\d+):seg(\d+)", ref)
            if match and (resolved := location(*(int(part) for part in match.groups()))):
                evidence.append(resolved)
        return {
            "id": key,
            "kind": kind,
            "type": str(value.get("type") or ""),
            "detail": str(value.get("detail") or value.get("explanation") or ""),
            "suggestion": str(value.get("suggestion") or ""),
            "status": _status(publications),
            "location": location(value.get("chapter"), value.get("index")),
            "evidence": evidence,
            "issue": value if kind == "issue" else {},
            "changes": suggestions,
            "publications": publications,
        }

    items = []
    for position, issue in enumerate(_rows(result.get("issues"))):
        detail = next((details[key] for key in sorted(_keys(issue)) if key in details), {})
        issue = {**detail, **issue}
        keys = _keys(issue)
        matched_changes = [i for i, change in enumerate(changes) if keys & _keys(change)]
        matched_records = [i for i, record in enumerate(records) if keys & _keys(record)]
        used_changes.update(matched_changes)
        used_records.update(matched_records)
        items.append(
            item(
                f"issue:{issue.get('issue_key') or issue.get('issue_id') or position}",
                "issue",
                issue,
                [changes[i] for i in matched_changes],
                [records[i] for i in matched_records],
            )
        )
    for position, change in enumerate(changes):
        if position in used_changes:
            continue
        matched = [
            i
            for i, record in enumerate(records)
            if i not in used_records
            and record.get("origin") == "change"
            and record.get("chapter") == change.get("chapter")
            and record.get("index") == change.get("index")
            and (
                record.get("after") == change.get("suggested_target")
                or bool(_keys(record) & _keys(change))
            )
        ]
        used_records.update(matched)
        items.append(
            item(f"change:{position}", "change", change, [change], [records[i] for i in matched])
        )
    for position, record in enumerate(records):
        if position not in used_records:
            items.append(
                item(
                    f"publication:{record.get('record_id') or position}",
                    "publication",
                    record,
                    [],
                    [record],
                )
            )
    return items
