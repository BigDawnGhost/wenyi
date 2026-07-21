"""``PostgresStorage`` —— Storage Protocol 的 Postgres 实现（Web 模式）。

实现与 :class:`wenyi_core.storage.file.FileStorage` 完全相同的接口，
但读写 Postgres 而非本地文件。内核（wenyi-core）通过注入的 Storage 操作它，
不感知后端。术语全文检索走 pg_trgm GIN 索引（<500ms）。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool
from wenyi_core.glossary.store import (
    CONFIDENCE_ORDER,
    GlossaryStore,
    GlossaryTerm,
)
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.storage import STATUS_DONE

_SOURCE_ONLY_TYPES = {t for t in (
    "称谓", "敬称", "口癖", "固定表达",
)}


class PostgresStorage:
    """Postgres 后端 Storage。一个实例绑定一个 project_id。"""

    def __init__(self, project_id: str, pool: ConnectionPool):
        self.project_id = project_id
        self._pool = pool

    @property
    def _conn(self):  # 便捷：返回上下文管理器
        return self._pool.connection()

    # ── 路径兼容属性（assemble / cli 风格访问；Web 模式无文件，给占位）─────
    @property
    def run_dir(self) -> str:
        return f"<postgres:{self.project_id}>"

    @property
    def glossary_path(self) -> str:
        return f"<postgres:{self.project_id}/glossary>"

    @property
    def report_path(self) -> str:
        return f"<postgres:{self.project_id}/report>"

    @property
    def manifest_path(self) -> str:
        return f"<postgres:{self.project_id}/manifest>"

    @property
    def event_log_path(self) -> str:
        return f"<postgres:{self.project_id}/events>"

    @property
    def usage_path(self) -> str:
        return f"<postgres:{self.project_id}/usage>"

    def close(self) -> None:
        # 连接池由全局管理，这里无需关闭。
        return None

    # ── 生命周期 ─────────────────────────────────────────────────────────
    def exists(self) -> bool:
        """项目是否已初始化（已有章节记录）。API 建项目时只建 project 行，
        不建章节；首次翻译时内核 prepare() 据此判定要不要跑完整初始化。"""
        with self._conn as conn:
            row = conn.execute(
                "SELECT 1 FROM chapters WHERE project_id = %s LIMIT 1",
                (self.project_id,),
            ).fetchone()
        return row is not None

    def init_from_document(self, doc: Document) -> dict:
        """按解析后的 Document 初始化：更新 project 元信息 + 写入章节/段落。"""
        pid = self.project_id
        with self._conn as conn:
            conn.execute(
                """UPDATE projects SET title=%s, fmt=%s, source_lang=%s,
                   target_lang=%s, meta=%s, source_path=%s, updated_at=now()
                   WHERE id=%s""",
                (doc.title, doc.fmt, doc.source_lang, doc.target_lang,
                 Json(doc.meta), doc.source_path, pid),
            )
            for ch in doc.chapters:
                self._upsert_chapter_row(conn, ch)
                self._replace_segments(conn, ch)
        return self.load_manifest()

    # ── manifest ─────────────────────────────────────────────────────────
    def load_manifest(self) -> dict:
        pid = self.project_id
        with self._conn as conn:
            prow = conn.execute(
                """SELECT title, fmt, source_path, source_lang, target_lang, meta
                   FROM projects WHERE id=%s""",
                (pid,),
            ).fetchone()
            if prow is None:
                raise KeyError(f"project {pid} not found")
            rows = conn.execute(
                """SELECT seq, title, href, status, title_translated
                   FROM chapters WHERE project_id=%s ORDER BY seq""",
                (pid,),
            ).fetchall()
        chapters = []
        for r in rows:
            c: dict[str, Any] = {
                "index": r[0], "title": r[1] or "",
                "href": r[2], "status": r[3] or "pending",
            }
            if r[4] is not None:
                c["title_translated"] = r[4]
            chapters.append(c)
        return {
            "title": prow[0], "fmt": prow[1], "source_path": prow[2],
            "source_lang": prow[3], "target_lang": prow[4],
            "meta": prow[5] or {}, "chapters": chapters,
        }

    def save_manifest(self, manifest: dict) -> None:
        pid = self.project_id
        with self._conn as conn:
            conn.execute(
                """UPDATE projects SET title=%s, fmt=%s, source_lang=%s,
                   target_lang=%s, meta=%s, updated_at=now() WHERE id=%s""",
                (manifest.get("title"), manifest.get("fmt"),
                 manifest.get("source_lang"), manifest.get("target_lang"),
                 Json(manifest.get("meta") or {}), pid),
            )
            for c in manifest.get("chapters", []):
                conn.execute(
                    """INSERT INTO chapters (project_id, seq, title, href, status, title_translated)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (project_id, seq) DO UPDATE
                       SET title=EXCLUDED.title, href=EXCLUDED.href,
                           status=EXCLUDED.status,
                           title_translated=EXCLUDED.title_translated""",
                    (pid, c.get("index"), c.get("title", ""), c.get("href"),
                     c.get("status", "pending"), c.get("title_translated")),
                )

    def set_chapter_status(self, ci: int, status: str) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE chapters SET status=%s WHERE project_id=%s AND seq=%s",
                (status, self.project_id, ci),
            )

    def pending_chapters(self) -> list[int]:
        with self._conn as conn:
            rows = conn.execute(
                """SELECT seq FROM chapters
                   WHERE project_id=%s AND status<>%s ORDER BY seq""",
                (self.project_id, STATUS_DONE),
            ).fetchall()
        return [r[0] for r in rows]

    # ── 章节 / 段落 ──────────────────────────────────────────────────────
    def _upsert_chapter_row(self, conn, ch: Chapter) -> None:
        conn.execute(
            """INSERT INTO chapters (project_id, seq, title, href, template, status, meta)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (project_id, seq) DO UPDATE
               SET title=EXCLUDED.title, href=EXCLUDED.href,
                   template=EXCLUDED.template, meta=EXCLUDED.meta""",
            (self.project_id, ch.index, ch.title, ch.href, ch.template,
             "pending", Json(ch.meta or {})),
        )

    def _replace_segments(self, conn, ch: Chapter) -> None:
        conn.execute(
            "DELETE FROM segments WHERE project_id=%s AND chapter_seq=%s",
            (self.project_id, ch.index),
        )
        for s in ch.segments:
            conn.execute(
                """INSERT INTO segments
                   (project_id, chapter_seq, seg_seq, source, target, kind, anchor, cont, meta)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (self.project_id, ch.index, s.index, s.source, s.target,
                 s.kind, s.anchor, s.cont, Json(s.meta or {})),
            )

    def save_chapter(self, chapter: Chapter) -> None:
        with self._conn as conn:
            self._upsert_chapter_row(conn, chapter)
            self._replace_segments(conn, chapter)

    def load_chapter(self, ci: int) -> Chapter:
        with self._conn as conn:
            crow = conn.execute(
                """SELECT title, href, template, meta, title_translated FROM chapters
                   WHERE project_id=%s AND seq=%s""",
                (self.project_id, ci),
            ).fetchone()
            if crow is None:
                raise KeyError(f"chapter {ci} not found in project {self.project_id}")
            srows = conn.execute(
                """SELECT seg_seq, source, target, kind, anchor, cont, meta
                   FROM segments WHERE project_id=%s AND chapter_seq=%s
                   ORDER BY seg_seq""",
                (self.project_id, ci),
            ).fetchall()
        segments = [
            Segment(index=r[0], source=r[1], target=r[2], kind=r[3] or "text",
                    anchor=r[4], cont=bool(r[5]), meta=r[6] or {})
            for r in srows
        ]
        return Chapter(index=ci, title=crow[0] or "", segments=segments,
                       href=crow[1], template=crow[2], meta=crow[3] or {},
                       title_translated=crow[4])

    # ── 上下文 / 分析 / 报告 / usage ─────────────────────────────────────
    def save_context(self, data: dict) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE projects SET context=%s WHERE id=%s",
                (Json(data), self.project_id),
            )

    def load_context(self) -> Optional[dict]:
        with self._conn as conn:
            row = conn.execute(
                "SELECT context FROM projects WHERE id=%s", (self.project_id,)
            ).fetchone()
        return row[0] if row else None

    def save_analysis(self, data: dict) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE projects SET analysis=%s WHERE id=%s",
                (Json(data), self.project_id),
            )

    def load_analysis(self) -> Optional[dict]:
        with self._conn as conn:
            row = conn.execute(
                "SELECT analysis FROM projects WHERE id=%s", (self.project_id,)
            ).fetchone()
        return row[0] if row else None

    def save_report(self, data: dict) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE projects SET report=%s WHERE id=%s",
                (Json(data), self.project_id),
            )

    def load_report(self) -> Optional[dict]:
        with self._conn as conn:
            row = conn.execute(
                "SELECT report FROM projects WHERE id=%s", (self.project_id,)
            ).fetchone()
        return row[0] if row else None

    def save_usage(self, data: dict) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE projects SET usage=%s WHERE id=%s",
                (Json(data), self.project_id),
            )

    def load_usage(self) -> Optional[dict]:
        with self._conn as conn:
            row = conn.execute(
                "SELECT usage FROM projects WHERE id=%s", (self.project_id,)
            ).fetchone()
        return row[0] if row else None

    # ── 事件日志 ─────────────────────────────────────────────────────────
    def log_event(self, event: str, **data: Any) -> None:
        with self._conn as conn:
            conn.execute(
                """INSERT INTO events (project_id, type, payload)
                   VALUES (%s,%s,%s)""",
                (self.project_id, event, Json(data)),
            )

    def list_events(self, *, event_type: Optional[str] = None,
                    limit: int = 200) -> list[dict]:
        with self._conn as conn:
            if event_type:
                rows = conn.execute(
                    """SELECT id, type, payload, created_at FROM events
                       WHERE project_id=%s AND type=%s
                       ORDER BY created_at DESC LIMIT %s""",
                    (self.project_id, event_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, type, payload, created_at FROM events
                       WHERE project_id=%s ORDER BY created_at DESC LIMIT %s""",
                    (self.project_id, limit),
                ).fetchall()
        out = []
        for r in rows:
            payload = dict(r[2] or {})
            payload.setdefault("event", r[1])
            payload["_id"] = r[0]
            payload["_ts"] = r[3].isoformat() if r[3] else None
            out.append(payload)
        return out

    # ── 术语表 ───────────────────────────────────────────────────────────
    # 术语 SELECT 列序：source,target,reading,type,gender,aliases,first_chapter,
    #                   note,confidence,locked,status
    def _row_to_term(self, row) -> GlossaryTerm:
        return GlossaryTerm(
            source=row[0], target=row[1], reading=row[2] or "",
            type=row[3] or "术语", gender=row[4] or "",
            aliases=list(row[5] or []), first_chapter=row[6],
            note=row[7] or "", confidence=row[8] or "medium",
            locked=bool(row[9]), status=row[10] or "ok",
        )

    _TERM_COLS = """source,target,reading,type,gender,aliases,first_chapter,
                    note,confidence,locked,status"""

    def _select_term(self, conn, source: str):
        return conn.execute(
            f"""SELECT {self._TERM_COLS} FROM glossary
               WHERE project_id=%s AND source=%s""",
            (self.project_id, source),
        ).fetchone()

    def get_term(self, source: str) -> Optional[GlossaryTerm]:
        with self._conn as conn:
            row = self._select_term(conn, source)
        return self._row_to_term(row) if row else None

    def upsert_term(self, term: GlossaryTerm,
                    chapter: Optional[int] = None) -> str:
        now = time.time()
        with self._conn as conn:
            existing = self._select_term(conn, term.source)
            if existing is None:
                conn.execute(
                    """INSERT INTO glossary
                       (project_id,source,target,reading,type,gender,aliases,
                        first_chapter,note,confidence,locked,status,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (self.project_id, term.source, term.target, term.reading,
                     term.type, term.gender, Json(term.aliases),
                     term.first_chapter if term.first_chapter is not None else chapter,
                     term.note, term.confidence, term.locked, term.status, now),
                )
                return "inserted"

            if existing[1] == term.target:
                merged = sorted(set(existing[5] or []) | set(term.aliases))
                conn.execute(
                    """UPDATE glossary
                       SET reading=COALESCE(NULLIF(%s,''),reading),
                           gender=COALESCE(NULLIF(%s,''),gender),
                           aliases=%s, note=COALESCE(NULLIF(%s,''),note),
                           updated_at=%s
                       WHERE project_id=%s AND source=%s""",
                    (term.reading, term.gender, Json(merged), term.note, now,
                     self.project_id, term.source),
                )
                return "unchanged"

            # target 不同 → 冲突判定
            existing_priority = (bool(existing[9]), CONFIDENCE_ORDER.get(existing[8], 1))
            new_priority = (term.locked, CONFIDENCE_ORDER.get(term.confidence, 1))
            self._log_conflict(conn, term.source, existing[1], term.target, chapter)
            if existing_priority >= new_priority:
                conn.execute(
                    """UPDATE glossary SET status='conflict', updated_at=%s
                       WHERE project_id=%s AND source=%s""",
                    (now, self.project_id, term.source),
                )
                return "conflict"
            conn.execute(
                """UPDATE glossary SET target=%s,
                   reading=COALESCE(NULLIF(%s,''),reading),
                   gender=COALESCE(NULLIF(%s,''),gender), confidence=%s,
                   status='conflict', updated_at=%s
                   WHERE project_id=%s AND source=%s""",
                (term.target, term.reading, term.gender, term.confidence, now,
                 self.project_id, term.source),
            )
            return "updated"

    def _log_conflict(self, conn, source, existing_target, proposed_target, chapter):
        conn.execute(
            """INSERT INTO term_conflicts
               (project_id,source,existing_target,proposed_target,chapter,created_at)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (self.project_id, source, existing_target, proposed_target,
             chapter, time.time()),
        )

    def delete_term(self, source: str) -> bool:
        with self._conn as conn:
            cur = conn.execute(
                "DELETE FROM glossary WHERE project_id=%s AND source=%s",
                (self.project_id, source),
            )
        return cur.rowcount > 0

    def lock_term(self, source: str, target: Optional[str] = None) -> None:
        with self._conn as conn:
            if target is not None:
                conn.execute(
                    """UPDATE glossary SET target=%s, locked=TRUE, confidence='high',
                       status='ok' WHERE project_id=%s AND source=%s""",
                    (target, self.project_id, source),
                )
            else:
                conn.execute(
                    """UPDATE glossary SET locked=TRUE, confidence='high', status='ok'
                       WHERE project_id=%s AND source=%s""",
                    (self.project_id, source),
                )

    def all_terms(self) -> list[GlossaryTerm]:
        with self._conn as conn:
            rows = conn.execute(
                """SELECT source,target,reading,type,gender,aliases,first_chapter,
                          note,confidence,locked,status FROM glossary
                   WHERE project_id=%s ORDER BY type, source""",
                (self.project_id,),
            ).fetchall()
        return [self._row_to_term(r) for r in rows]

    def terms_in(self, terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        return GlossaryStore.terms_in(terms, text)

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        return GlossaryStore.terms_in(self.all_terms(), text)

    def mark_conflicts_resolved(self, source: str) -> None:
        with self._conn as conn:
            conn.execute(
                """UPDATE term_conflicts SET resolved=TRUE
                   WHERE project_id=%s AND source=%s""",
                (self.project_id, source),
            )

    def open_conflicts(self) -> list[dict]:
        with self._conn as conn:
            rows = conn.execute(
                """SELECT id, source, existing_target, proposed_target, chapter, note
                   FROM term_conflicts
                   WHERE project_id=%s AND resolved=FALSE ORDER BY created_at""",
                (self.project_id,),
            ).fetchall()
        return [
            {"id": r[0], "source": r[1], "existing_target": r[2],
             "proposed_target": r[3], "chapter": r[4], "note": r[5]}
            for r in rows
        ]

    def low_confidence_terms(self) -> list[GlossaryTerm]:
        with self._conn as conn:
            rows = conn.execute(
                """SELECT source,target,reading,type,gender,aliases,first_chapter,
                          note,confidence,locked,status FROM glossary
                   WHERE project_id=%s AND (confidence='low' OR status='conflict')
                   ORDER BY source""",
                (self.project_id,),
            ).fetchall()
        return [self._row_to_term(r) for r in rows]

    # ── 翻译记忆库 ───────────────────────────────────────────────────────
    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def add_tm(self, source_text: str, target_text: str,
               chapter: Optional[int] = None) -> None:
        with self._conn as conn:
            conn.execute(
                """INSERT INTO translation_memory
                   (project_id,source_hash,source_text,target_text,chapter,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (project_id,source_hash) DO UPDATE
                   SET target_text=EXCLUDED.target_text, chapter=EXCLUDED.chapter,
                       updated_at=EXCLUDED.updated_at""",
                (self.project_id, self._hash(source_text), source_text, target_text,
                 chapter, time.time()),
            )

    def tm_lookup(self, source_text: str) -> Optional[str]:
        with self._conn as conn:
            row = conn.execute(
                """SELECT target_text FROM translation_memory
                   WHERE project_id=%s AND source_hash=%s""",
                (self.project_id, self._hash(source_text)),
            ).fetchone()
        return row[0] if row else None

    # ── 统计 ─────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        with self._conn as conn:
            g = conn.execute(
                "SELECT COUNT(*) FROM glossary WHERE project_id=%s",
                (self.project_id,),
            ).fetchone()[0]
            c = conn.execute(
                """SELECT COUNT(*) FROM term_conflicts
                   WHERE project_id=%s AND resolved=FALSE""",
                (self.project_id,),
            ).fetchone()[0]
            t = conn.execute(
                "SELECT COUNT(*) FROM translation_memory WHERE project_id=%s",
                (self.project_id,),
            ).fetchone()[0]
        return {"terms": g, "open_conflicts": c, "tm_entries": t}
