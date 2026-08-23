"""SQLite 术语库。

两张表：
- glossary：专有名词对照表（source 唯一）。同 source 出现不同 target 时保留当前
  译法，并把候选译法记入 term_conflicts，等待人工裁决。
- term_conflicts：待裁决的译法冲突日志，供人工复核。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# 术语类型
TYPE_PERSON = "人物"
TYPE_TERM = "术语"
TYPE_APPELLATION = "称谓"
TYPE_HONORIFIC = "敬称"
TYPE_SPEECH = "口癖"
TYPE_FIXED_EXPR = "固定表达"

_SOURCE_ONLY_TYPES = {TYPE_APPELLATION, TYPE_HONORIFIC, TYPE_SPEECH, TYPE_FIXED_EXPR}


@dataclass
class GlossaryTerm:
    source: str
    target: str
    reading: str = ""
    type: str = TYPE_TERM
    gender: str = ""
    aliases: list[str] = field(default_factory=list)
    first_chapter: int | None = None
    note: str = ""
    status: str = "ok"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> GlossaryTerm:
        """把 SQLite 行转换为术语对象，并恢复 JSON 编码的别名。"""
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

_SCHEMA = (
    _CREATE_GLOSSARY_TABLE
    + ";"
    + """
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
"""
)


# 与 epub_reader 写入 Segment.source 的振假名标记一致；匹配前剥离以免拆散子串。
_RUBY_MARK_RE = re.compile(r"〘[^〙]*〙")


def _match_text(text: str) -> str:
    """Normalize width/compatibility forms and case for glossary matching.

    同时去掉正文里的振假名标记 ``漢字〘かんじ〙`` → ``漢字``，否则
    ``与り`` 无法命中 ``与〘あずか〙り``。
    """
    if "〘" in text:
        text = _RUBY_MARK_RE.sub("", text)
    return unicodedata.normalize("NFKC", text).casefold()


_WORD_BOUNDARY_SCRIPTS = ("LATIN", "GREEK", "CYRILLIC")


def _source_pattern(key: str) -> re.Pattern[str] | None:
    """为空格分词文字构造边界正则；连续书写文字返回 None 使用子串匹配。"""
    if key.isascii():
        return re.compile(rf"(?<![a-z0-9_]){re.escape(key)}(?![a-z0-9_])")

    letters = [char for char in key if char.isalpha()]
    if not letters or not all(
        any(script in unicodedata.name(char, "") for script in _WORD_BOUNDARY_SCRIPTS)
        for char in letters
    ):
        return None

    left_boundary = r"(?<!\w)" if key[0].isalnum() else ""
    right_boundary = r"(?!\w)" if key[-1].isalnum() else ""
    return re.compile(f"{left_boundary}{re.escape(key)}{right_boundary}")


def source_matches_text(source: str, text: str) -> bool:
    """判断术语原文是否出现，并避免空格分词文字命中更长单词。

    CJK 等连续书写文字沿用规范化子串匹配；ASCII、拉丁、希腊和西里尔文字
    检查单词边界，避免 ``Ann`` 命中 ``Anna``、``гад`` 命中 ``гадкий``。
    """
    key = _match_text(source).strip()
    if not key:
        return False
    normalized_text = _match_text(text)
    if pattern := _source_pattern(key):
        return pattern.search(normalized_text) is not None
    return key in normalized_text


def term_match_sources(term: GlossaryTerm) -> list[str]:
    """返回术语在源文中的允许匹配写法。

    称谓、敬称、口癖和固定表达只按完整 source 匹配，避免其裸名 alias
    把带语气/场景的派生译法注入普通称呼；其它实体可同时匹配 aliases。
    """
    if term.type in _SOURCE_ONLY_TYPES:
        return [term.source]
    return [term.source, *term.aliases]


def _source_occurrence_spans(source: str, normalized_text: str) -> list[tuple[int, int]]:
    """返回术语在已规范化文本中的非重叠命中区间。"""
    key = _match_text(source).strip()
    if not key:
        return []
    if pattern := _source_pattern(key):
        return [match.span() for match in pattern.finditer(normalized_text)]

    spans: list[tuple[int, int]] = []
    start = 0
    while (index := normalized_text.find(key, start)) != -1:
        end = index + len(key)
        spans.append((index, end))
        start = end
    return spans


def _merged_occurrence_count(spans: set[tuple[int, int]]) -> int:
    """把 source 与 alias 在同一处产生的重叠命中合并为一次正文提及。"""
    count = 0
    active_end = -1
    for start, end in sorted(spans):
        if start >= active_end:
            count += 1
            active_end = end
        else:
            active_end = max(active_end, end)
    return count


class GlossaryOccurrenceMatcher:
    """复用一份规范化全文，按 source/alias 判断术语是否重复出现。"""

    def __init__(self, text: str):
        self.normalized_text = _match_text(text)

    def recurring_terms(
        self,
        terms: list[GlossaryTerm],
        *,
        min_occurrences: int = 2,
    ) -> list[GlossaryTerm]:
        """筛出 source/alias 在全文累计出现至少指定次数的术语。"""
        if min_occurrences <= 1:
            return GlossaryStore.terms_in(terms, self.normalized_text)

        recurring: list[GlossaryTerm] = []
        for term in terms:
            raw_keys = term_match_sources(term)
            keys = {normalized for key in raw_keys if (normalized := _match_text(key).strip())}
            spans: set[tuple[int, int]] = set()
            for key in keys:
                spans.update(_source_occurrence_spans(key, self.normalized_text))
            if _merged_occurrence_count(spans) >= min_occurrences:
                recurring.append(term)
        return recurring


class GlossaryStore:
    def __init__(self, db_path: str):
        """打开术语数据库并初始化当前版本的表结构。"""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # 并发写等待，避免 Web 编辑与翻译 worker 同写时报 "database is locked"
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        self.conn.close()

    @classmethod
    def load_terms_readonly(cls, db_path: str) -> list[GlossaryTerm]:
        """从临时文件快照读取术语，不触碰正式数据库及其 WAL 锁文件。

        ``immutable=1`` 虽不会创建 ``-shm``，却会忽略尚未 checkpoint 的
        已提交 WAL。这里把数据库和现存 WAL 复制到临时目录，再让 SQLite
        在副本上恢复完整视图；副本产生的 ``-shm``/checkpoint 也不会污染
        书籍的正式状态目录。
        """
        with tempfile.TemporaryDirectory(prefix="wenyi-glossary-review-") as directory:
            snapshot_path = f"{directory}/glossary.db"
            wal_path = f"{db_path}-wal"
            snapshot_wal_path = f"{snapshot_path}-wal"

            def signature(path: str) -> tuple[int, int, int, int] | None:
                """返回足以发现复制期间文件变化的轻量签名。"""
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    return None
                return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

            # DB 与 WAL 是两个文件，不能把一次顺序 copy 当作原子快照。复制前后
            # 签名一致才接受；若恰逢 checkpoint/写入则丢弃副本并重试。
            for _attempt in range(5):
                before = signature(db_path), signature(wal_path)
                try:
                    shutil.copy2(db_path, snapshot_path)
                    if before[1] is not None:
                        shutil.copy2(wal_path, snapshot_wal_path)
                    elif os.path.exists(snapshot_wal_path):
                        os.unlink(snapshot_wal_path)
                except FileNotFoundError:
                    continue
                after = signature(db_path), signature(wal_path)
                if before == after:
                    break
            else:
                raise RuntimeError("术语库在只读快照期间持续变化，请稍后重试 Review")

            conn = sqlite3.connect(snapshot_path)
            conn.row_factory = sqlite3.Row
            try:
                # 按入库顺序（rowid）返回，而非按 type/source 字母序：后者会让新增词条
                # 插进已有列表中间，使注入 prompt 的对照表整体错位，白白打掉前缀缓存。
                rows = conn.execute("SELECT * FROM glossary ORDER BY rowid").fetchall()
                return [GlossaryTerm.from_row(row) for row in rows]
            finally:
                conn.close()

    # ── 术语 ──────────────────────────────────────────────────────────────
    def get_term(self, source: str) -> GlossaryTerm | None:
        """按原文精确查询术语；不存在时返回 None。"""
        row = self.conn.execute("SELECT * FROM glossary WHERE source = ?", (source,)).fetchone()
        return GlossaryTerm.from_row(row) if row else None

    def upsert_term(self, term: GlossaryTerm, chapter: int | None = None) -> str:
        """插入或更新术语，返回 'inserted'|'unchanged'|'conflict'。

        同 source 已存在且 target 不同时保留当前译法，把新译法作为候选记录，
        避免自动提取结果在无人确认时改写术语表。
        """
        try:
            # 锁在读取 existing 之前取得，保证两个连接不会同时基于旧快照决策。
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.get_term(term.source)
            now = time.time()
            if existing is None:
                self.conn.execute(
                    """INSERT INTO glossary
                       (source,target,reading,type,gender,aliases,first_chapter,note,
                        status,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        term.source,
                        term.target,
                        term.reading,
                        term.type,
                        term.gender,
                        json.dumps(term.aliases, ensure_ascii=False),
                        term.first_chapter if term.first_chapter is not None else chapter,
                        term.note,
                        term.status,
                        now,
                    ),
                )
                result = "inserted"
            elif existing.target == term.target:
                # 合并别名 / 补全字段，不算冲突
                merged_aliases = sorted(set(existing.aliases) | set(term.aliases))
                self.conn.execute(
                    """UPDATE glossary SET reading=COALESCE(NULLIF(?,''),reading),
                       gender=COALESCE(NULLIF(?,''),gender), aliases=?,
                       note=COALESCE(NULLIF(?,''),note), updated_at=? WHERE source=?""",
                    (
                        term.reading,
                        term.gender,
                        json.dumps(merged_aliases, ensure_ascii=False),
                        term.note,
                        now,
                        term.source,
                    ),
                )
                result = "unchanged"
            else:
                # target 不同：保留当前译法，记录候选译法等待人工裁决。
                self._log_conflict(term.source, existing.target, term.target, chapter)
                self.conn.execute(
                    "UPDATE glossary SET status='conflict', updated_at=? WHERE source=?",
                    (now, term.source),
                )
                result = "conflict"
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def _log_conflict(self, source, existing_target, proposed_target, chapter):
        """在当前事务中记录一次候选译法冲突。"""
        self.conn.execute(
            """INSERT INTO term_conflicts
               (source,existing_target,proposed_target,chapter,created_at)
               VALUES (?,?,?,?,?)""",
            (source, existing_target, proposed_target, chapter, time.time()),
        )

    def resolve_term(self, source: str, target: str) -> bool:
        """人工裁定最终译法并恢复正常状态，返回术语是否存在。"""
        cur = self.conn.execute(
            "UPDATE glossary SET target=?, status='ok', updated_at=? WHERE source=?",
            (target, time.time(), source),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def all_terms(self) -> list[GlossaryTerm]:
        """按入库顺序（rowid）返回全部术语。

        这是几乎所有翻译/审校/润色 prompt 注入术语表的唯一数据源：按 rowid
        排序保证既有词条位置恒定，新词条只会追加在末尾，改动最小化才能让
        DeepSeek 等按前缀匹配的缓存持续命中；不得改回按 type/source 字母序
        （新词插入表中间会让后面所有词条错位，整段前缀失效）。人类浏览用的
        字母序分组排序应在展示层（如 CLI）自行 sorted()，不要动这里。
        """
        rows = self.conn.execute("SELECT * FROM glossary ORDER BY rowid").fetchall()
        return [GlossaryTerm.from_row(r) for r in rows]

    @staticmethod
    def terms_in(terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        """从给定术语列表里筛出 source 或任一别名在 text 中出现的项。

        与 terms_in_text 同义，但接受预取的术语快照，避免逐批重复查库（章内术语表不变）。
        """
        out: list[GlossaryTerm] = []
        normalized_text = _match_text(text)
        for term in terms:
            # 称谓/口癖/固定表达是带语气或场景的派生写法，不能因为 alias
            # 命中裸名就把派生译法注入到普通称呼处。
            keys = term_match_sources(term)
            if any(source_matches_text(k, normalized_text) for k in keys):
                out.append(term)
        return out

    @staticmethod
    def recurring_terms(
        terms: list[GlossaryTerm],
        text: str,
        *,
        min_occurrences: int = 2,
    ) -> list[GlossaryTerm]:
        """筛出 source/alias 在全文累计出现至少指定次数的术语。

        称谓、敬称、口癖和固定表达只按 source 统计，避免裸名 alias 让派生表达
        被误判为高频。其它类型会合并 source 与去重后的 aliases 出现次数。
        """
        return GlossaryOccurrenceMatcher(text).recurring_terms(
            terms,
            min_occurrences=min_occurrences,
        )

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        """返回 source 或任一别名在 text 中出现的术语（注入翻译 prompt 用）。"""
        return self.terms_in(self.all_terms(), text)

    def mark_conflicts_resolved(self, source: str) -> None:
        """把指定原文术语的全部未决冲突标记为已处理。"""
        self.conn.execute("UPDATE term_conflicts SET resolved=1 WHERE source=?", (source,))
        self.conn.commit()

    def open_conflicts(self) -> list[dict[str, Any]]:
        """按发生时间返回仍待人工裁决的冲突记录。"""
        rows = self.conn.execute(
            "SELECT * FROM term_conflicts WHERE resolved=0 ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        """返回术语数和未决冲突数。"""
        g = self.conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
        c = self.conn.execute("SELECT COUNT(*) FROM term_conflicts WHERE resolved=0").fetchone()[0]
        return {"terms": g, "open_conflicts": c}
