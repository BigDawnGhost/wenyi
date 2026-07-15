"""SQLite 术语库 + 翻译记忆库。

三张表：
- glossary：专有名词对照表（source 唯一）。同 source 出现不同 target 时保留当前
  译法，并把候选译法记入 term_conflicts，等待人工裁决。
- term_conflicts：待裁决的译法冲突日志，供人工复核。
- translation_memory：句群级译文对，供一致性参考与重译复用。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# 术语类型
TYPE_PERSON = "人物"
TYPE_PLACE = "地名"
TYPE_ORG = "组织"
TYPE_TERM = "术语"
TYPE_SKILL = "招式"
TYPE_APPELLATION = "称谓"
TYPE_HONORIFIC = "敬称"
TYPE_SPEECH = "口癖"
TYPE_FIXED_EXPR = "固定表达"
TYPE_ONOMATOPOEIA = "拟声词"

_SOURCE_ONLY_TYPES = {TYPE_APPELLATION, TYPE_HONORIFIC, TYPE_SPEECH, TYPE_FIXED_EXPR}

@dataclass
class GlossaryTerm:
    source: str
    target: str
    reading: str = ""
    type: str = TYPE_TERM
    gender: str = ""
    aliases: list[str] = field(default_factory=list)
    first_chapter: Optional[int] = None
    note: str = ""
    status: str = "ok"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GlossaryTerm":
        return cls(
            source=row["source"],
            target=row["target"],
            reading=row["reading"] or "",
            type=row["type"] or TYPE_TERM,
            gender=row["gender"] or "",
            aliases=json.loads(row["aliases"] or "[]"),
            first_chapter=row["first_chapter"],
            note=row["note"] or "",
            status=row["status"] or "ok",
        )


_CREATE_GLOSSARY_TABLE = """
CREATE TABLE IF NOT EXISTS glossary (
    source        TEXT PRIMARY KEY,
    target        TEXT NOT NULL,
    reading       TEXT,
    type          TEXT,
    gender        TEXT,
    aliases       TEXT,
    first_chapter INTEGER,
    note          TEXT,
    status        TEXT DEFAULT 'ok',
    updated_at    REAL
)
"""

_SCHEMA = _CREATE_GLOSSARY_TABLE + ";" + """
CREATE TABLE IF NOT EXISTS term_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    existing_target TEXT,
    proposed_target TEXT,
    chapter         INTEGER,
    note            TEXT,
    resolved        INTEGER DEFAULT 0,
    created_at      REAL
);
CREATE TABLE IF NOT EXISTS translation_memory (
    source_hash TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    chapter     INTEGER,
    updated_at  REAL
);
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class GlossaryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # 并发写等待，避免 Web 编辑与翻译 worker 同写时报 "database is locked"
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        self._migrate_legacy_glossary_schema()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _migrate_legacy_glossary_schema(self) -> None:
        """从旧库移除 confidence/locked 字段，同时保留全部术语数据。"""
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(glossary)").fetchall()
        }
        if not {"confidence", "locked"} & columns:
            return

        legacy_table = "glossary_legacy_priority"
        if self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (legacy_table,),
        ).fetchone():
            raise RuntimeError(f"术语库迁移失败：临时表 {legacy_table} 已存在")

        preserved = (
            "source,target,reading,type,gender,aliases,first_chapter,note,status,updated_at"
        )
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(f"ALTER TABLE glossary RENAME TO {legacy_table}")
            self.conn.execute(_CREATE_GLOSSARY_TABLE)
            self.conn.execute(
                f"INSERT INTO glossary ({preserved}) "
                f"SELECT {preserved} FROM {legacy_table}"
            )
            self.conn.execute(f"DROP TABLE {legacy_table}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ── 术语 ──────────────────────────────────────────────────────────────
    def get_term(self, source: str) -> Optional[GlossaryTerm]:
        row = self.conn.execute(
            "SELECT * FROM glossary WHERE source = ?", (source,)
        ).fetchone()
        return GlossaryTerm.from_row(row) if row else None

    def upsert_term(self, term: GlossaryTerm, chapter: Optional[int] = None) -> str:
        """插入或更新术语，返回 'inserted'|'unchanged'|'conflict'。

        同 source 已存在且 target 不同时保留当前译法，把新译法作为候选记录，
        避免自动提取结果在无人确认时改写术语表。
        """
        existing = self.get_term(term.source)
        now = time.time()
        if existing is None:
            self.conn.execute(
                """INSERT INTO glossary
                   (source,target,reading,type,gender,aliases,first_chapter,note,
                    status,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    term.source, term.target, term.reading, term.type, term.gender,
                    json.dumps(term.aliases, ensure_ascii=False),
                    term.first_chapter if term.first_chapter is not None else chapter,
                    term.note, term.status, now,
                ),
            )
            self.conn.commit()
            return "inserted"

        if existing.target == term.target:
            # 合并别名 / 补全字段，不算冲突
            merged_aliases = sorted(set(existing.aliases) | set(term.aliases))
            self.conn.execute(
                """UPDATE glossary SET reading=COALESCE(NULLIF(?,''),reading),
                   gender=COALESCE(NULLIF(?,''),gender), aliases=?, note=COALESCE(NULLIF(?,''),note),
                   updated_at=? WHERE source=?""",
                (term.reading, term.gender, json.dumps(merged_aliases, ensure_ascii=False),
                 term.note, now, term.source),
            )
            self.conn.commit()
            return "unchanged"

        # target 不同：保留当前译法，记录候选译法等待人工裁决。
        self._log_conflict(term.source, existing.target, term.target, chapter)
        self.conn.execute(
            "UPDATE glossary SET status='conflict', updated_at=? WHERE source=?",
            (now, term.source),
        )
        self.conn.commit()
        return "conflict"

    def _log_conflict(self, source, existing_target, proposed_target, chapter):
        self.conn.execute(
            """INSERT INTO term_conflicts
               (source,existing_target,proposed_target,chapter,created_at)
               VALUES (?,?,?,?,?)""",
            (source, existing_target, proposed_target, chapter, time.time()),
        )

    def delete_term(self, source: str) -> bool:
        """删除一个术语条目（前端编辑用）。返回是否确有删除。"""
        cur = self.conn.execute("DELETE FROM glossary WHERE source = ?", (source,))
        self.conn.commit()
        return cur.rowcount > 0

    def resolve_term(self, source: str, target: str) -> bool:
        """人工裁定最终译法并恢复正常状态，返回术语是否存在。"""
        cur = self.conn.execute(
            "UPDATE glossary SET target=?, status='ok', updated_at=? WHERE source=?",
            (target, time.time(), source),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def all_terms(self) -> list[GlossaryTerm]:
        rows = self.conn.execute(
            "SELECT * FROM glossary ORDER BY type, source"
        ).fetchall()
        return [GlossaryTerm.from_row(r) for r in rows]

    @staticmethod
    def terms_in(terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        """从给定术语列表里筛出 source 或任一别名在 text 中出现的项。

        与 terms_in_text 同义，但接受预取的术语快照，避免逐批重复查库（章内术语表不变）。
        """
        out: list[GlossaryTerm] = []
        for term in terms:
            # 称谓/口癖/固定表达是带语气或场景的派生写法，不能因为 alias
            # 命中裸名就把派生译法注入到普通称呼处。
            keys = (
                [term.source]
                if term.type in _SOURCE_ONLY_TYPES
                else [term.source] + term.aliases
            )
            if any(k and k in text for k in keys):
                out.append(term)
        return out

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        """返回 source 或任一别名在 text 中出现的术语（注入翻译 prompt 用）。"""
        return self.terms_in(self.all_terms(), text)

    def mark_conflicts_resolved(self, source: str) -> None:
        self.conn.execute(
            "UPDATE term_conflicts SET resolved=1 WHERE source=?", (source,)
        )
        self.conn.commit()

    def open_conflicts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM term_conflicts WHERE resolved=0 ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 翻译记忆库 ──────────────────────────────────────────────────────
    def add_tm(self, source_text: str, target_text: str, chapter: Optional[int] = None) -> None:
        self.conn.execute(
            """INSERT INTO translation_memory (source_hash,source_text,target_text,chapter,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(source_hash) DO UPDATE SET target_text=excluded.target_text,
                   chapter=excluded.chapter, updated_at=excluded.updated_at""",
            (_hash(source_text), source_text, target_text, chapter, time.time()),
        )
        self.conn.commit()

    def tm_lookup(self, source_text: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT target_text FROM translation_memory WHERE source_hash=?",
            (_hash(source_text),),
        ).fetchone()
        return row["target_text"] if row else None

    def stats(self) -> dict[str, int]:
        g = self.conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
        c = self.conn.execute("SELECT COUNT(*) FROM term_conflicts WHERE resolved=0").fetchone()[0]
        t = self.conn.execute("SELECT COUNT(*) FROM translation_memory").fetchone()[0]
        return {"terms": g, "open_conflicts": c, "tm_entries": t}
