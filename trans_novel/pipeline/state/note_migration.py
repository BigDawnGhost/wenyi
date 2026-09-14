"""持久化与源文件绑定的注释槽位迁移，支持崩溃恢复。"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from trans_novel.ingest import Chapter
from trans_novel.pipeline.state.models import (
    NODE_PENDING,
    NODE_SUCCEEDED,
    NodeState,
    stable_digest,
)

_VERSION = 1
_PREFIX = "EPUB note migration rejected:"


def _reject(detail: str) -> ValueError:
    return ValueError(f"{_PREFIX} {detail}")


def _journal_path(store) -> str:
    return store.path_for("note_migration.json")


def _backup_path(store, name: str) -> str:
    return store.path_for(os.path.join("note_migration_backup", name))


def _read_record(store) -> dict[str, Any] | None:
    path = _journal_path(store)
    if not os.path.isfile(path):
        return None
    try:
        value = store.read_json(path)
    except (OSError, ValueError, TypeError) as exc:
        raise _reject("invalid migration journal") from exc
    if not isinstance(value, dict) or value.get("version") != _VERSION:
        raise _reject("invalid migration journal")
    return value


def _payload(record: dict[str, Any], name: str) -> dict[str, Any]:
    value = record.get(name)
    if not isinstance(value, dict) or stable_digest(value.get("data")) != value.get("sha256"):
        raise _reject(f"invalid journal {name}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise _reject(f"invalid journal {name}")
    return data


def _chapters(record: dict[str, Any]) -> list[dict[str, Any]]:
    values = record.get("chapters")
    if not isinstance(values, list):
        raise _reject("invalid journal chapters")
    parsed: list[dict[str, Any]] = []
    for value in values:
        if (
            not isinstance(value, dict)
            or type(value.get("index")) is not int
            or not isinstance(value.get("before"), dict)
            or not isinstance(value.get("after"), dict)
        ):
            raise _reject("invalid journal chapter")
        before, after = value["before"], value["after"]
        if stable_digest(before.get("data")) != before.get("sha256") or stable_digest(
            after.get("data")
        ) != after.get("sha256"):
            raise _reject("invalid journal chapter digest")
        if not isinstance(before.get("data"), dict) or not isinstance(after.get("data"), dict):
            raise _reject("invalid journal chapter payload")
        if (
            before["data"].get("index") != value["index"]
            or after["data"].get("index") != value["index"]
        ):
            raise _reject("invalid journal chapter index")
        encoded = value.get("before_bytes_base64")
        try:
            original = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as exc:
            raise _reject("invalid journal chapter backup") from exc
        if hashlib.sha256(original).hexdigest() != value.get("before_file_sha256"):
            raise _reject("invalid journal chapter backup digest")
        parsed.append(value)
    return parsed


def _source_hash(manifest: dict[str, Any]) -> str:
    identity = manifest.get("identity")
    value = identity.get("source_bytes_sha256") if isinstance(identity, dict) else None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _reject("manifest has no source binding")
    return value


def _ensure_backup(store, record: dict[str, Any]) -> None:
    source = record.get("source_bytes_sha256")
    entries = [("manifest_meta.json", _payload(record, "before_meta"))]
    entries.extend(
        (
            f"ch{item['index']}.json",
            {
                "chapter_bytes_base64": item["before_bytes_base64"],
                "chapter_sha256": item["before_file_sha256"],
            },
        )
        for item in _chapters(record)
    )
    missing: list[tuple[str, dict[str, Any]]] = []
    for name, data in entries:
        path = _backup_path(store, name)
        wrapped = {"version": _VERSION, "source_bytes_sha256": source, "data": data}
        if not os.path.isfile(path):
            missing.append((path, wrapped))
            continue
        try:
            existing = store.read_json(path)
        except (OSError, ValueError, TypeError) as exc:
            raise _reject(f"invalid migration backup {name}") from exc
        if existing != wrapped:
            raise _reject(f"conflicting migration backup {name}")
    for path, wrapped in missing:
        store.write_json(path, wrapped)


def _validate_current(store, record: dict[str, Any]) -> None:
    try:
        manifest = store.read_json(store.manifest_path)
    except (OSError, ValueError, TypeError) as exc:
        raise _reject("invalid manifest during migration") from exc
    if not isinstance(manifest, dict) or _source_hash(manifest) != record.get(
        "source_bytes_sha256"
    ):
        raise _reject("journal source binding does not match manifest")
    allowed_manifest = {
        record.get("before_manifest", {}).get("sha256"),
        record.get("after_manifest", {}).get("sha256"),
    }
    if stable_digest(manifest) not in allowed_manifest:
        raise _reject("manifest changed during migration")
    for item in _chapters(record):
        try:
            current = store.read_json(store.chapter_path(item["index"]))
        except (OSError, ValueError, TypeError) as exc:
            raise _reject(f"invalid chapter {item['index']} during migration") from exc
        if stable_digest(current) not in {
            item["before"]["sha256"],
            item["after"]["sha256"],
        }:
            raise _reject(f"chapter {item['index']} changed during migration")


def recover_note_migration(store) -> None:
    """幂等地完成日志中已记录的迁移；调用方必须持有运行锁。"""
    record = _read_record(store)
    if record is None:
        return
    before_meta = _payload(record, "before_meta")
    after_meta = _payload(record, "after_meta")
    before_manifest = _payload(record, "before_manifest")
    after_manifest = _payload(record, "after_manifest")
    if before_manifest.get("meta") != before_meta or after_manifest.get("meta") != after_meta:
        raise _reject("journal manifest metadata mismatch")
    source = record.get("source_bytes_sha256")
    if _source_hash(before_manifest) != source or _source_hash(after_manifest) != source:
        raise _reject("journal manifest source binding mismatch")
    _chapters(record)
    _validate_current(store, record)
    _ensure_backup(store, record)
    for item in _chapters(record):
        store.write_json(store.chapter_path(item["index"]), item["after"]["data"])
    store.write_json(store.manifest_path, after_manifest)
    os.remove(_journal_path(store))


def _updated_manifest(
    before: dict[str, Any],
    chapters: list[Chapter],
    meta: dict[str, Any],
    fingerprint_updates: dict[str, tuple[str, str]] | None,
) -> dict[str, Any]:
    after = dict(before)
    after["meta"] = meta
    by_index = {chapter.index: chapter for chapter in chapters}
    indexes = before.get("chapters")
    assert isinstance(indexes, list)
    after["chapters"] = [
        {
            **item,
            "processing": (
                by_index[item["index"]].processing.model_dump(mode="json")
                if by_index[item["index"]].processing is not None
                else None
            ),
        }
        for item in indexes
    ]
    nodes = dict(before.get("nodes") or {})
    for key, pair in (fingerprint_updates or {}).items():
        node = nodes.get(key)
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(node, dict)
            or node.get("status") != NODE_SUCCEEDED
            or node.get("input_fingerprint") != pair[0]
        ):
            raise _reject(f"content fingerprint changed independently at {key}")
        nodes[key] = {**node, "input_fingerprint": pair[1]}
    for key in ("report", "assemble"):
        if key in nodes:
            nodes[key] = NodeState(node_id=key, status=NODE_PENDING).model_dump(mode="json")
    after["nodes"] = nodes
    return after


def commit_note_migration(
    store,
    *,
    chapters: list[Chapter],
    meta: dict,
    fingerprint_updates: dict[str, tuple[str, str]] | None = None,
) -> None:
    """记录并提交已完成验证的迁移；调用方必须持有运行锁。"""
    recover_note_migration(store)
    before_manifest = store.read_json(store.manifest_path)
    if not isinstance(before_manifest, dict):
        raise _reject("invalid manifest")
    source = _source_hash(before_manifest)
    indexes = before_manifest.get("chapters")
    expected = [item.get("index") for item in indexes] if isinstance(indexes, list) else None
    if expected != [chapter.index for chapter in chapters]:
        raise _reject("chapter index set changed")
    after_manifest = _updated_manifest(before_manifest, chapters, dict(meta), fingerprint_updates)
    changed: list[dict[str, Any]] = []
    for chapter in chapters:
        before = store.read_json(store.chapter_path(chapter.index))
        after = chapter.to_dict()
        with open(store.chapter_path(chapter.index), "rb") as stream:
            before_bytes = stream.read()
        if stable_digest(before) != stable_digest(after):
            changed.append(
                {
                    "index": chapter.index,
                    "before": {"sha256": stable_digest(before), "data": before},
                    "after": {"sha256": stable_digest(after), "data": after},
                    "before_bytes_base64": base64.b64encode(before_bytes).decode("ascii"),
                    "before_file_sha256": hashlib.sha256(before_bytes).hexdigest(),
                }
            )
    if not changed and before_manifest == after_manifest:
        return
    record = {
        "version": _VERSION,
        "source_bytes_sha256": source,
        "before_meta": {
            "sha256": stable_digest(before_manifest.get("meta")),
            "data": before_manifest.get("meta"),
        },
        "after_meta": {"sha256": stable_digest(meta), "data": meta},
        "before_manifest": {
            "sha256": stable_digest(before_manifest),
            "data": before_manifest,
        },
        "after_manifest": {"sha256": stable_digest(after_manifest), "data": after_manifest},
        "chapters": changed,
    }
    _ensure_backup(store, record)
    store.write_json(_journal_path(store), record)
    for item in changed:
        store.write_json(store.chapter_path(item["index"]), item["after"]["data"])
    store.write_json(store.manifest_path, after_manifest)
    os.remove(_journal_path(store))


__all__ = ["commit_note_migration", "recover_note_migration"]
