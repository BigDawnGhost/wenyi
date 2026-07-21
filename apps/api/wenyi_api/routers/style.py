"""风格分析 & 书籍概要：查看 / 编辑（翻译前准备的产物）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from psycopg.types.json import Json

from ..db import get_pool
from ..schemas import AnalysisUpdate
from ..storage_pg import PostgresStorage

router = APIRouter(prefix="/projects/{pid}", tags=["style"])


@router.get("/analysis")
def get_analysis(pid: str) -> dict:
    with get_pool().connection() as c:
        r = c.execute("SELECT analysis FROM projects WHERE id=%s", (pid,)).fetchone()
    if r is None:
        raise HTTPException(404, "project not found")
    analysis = r[0] or {}
    # 章节摘要：从各章 meta.source_digest 汇总
    storage = PostgresStorage(pid, get_pool())
    m = storage.load_manifest()
    digests = []
    for ch in m.get("chapters", []):
        try:
            loaded = storage.load_chapter(ch["index"])
        except KeyError:
            continue
        d = (loaded.meta or {}).get("source_digest", "")
        digests.append({"index": ch["index"], "title": ch.get("title", ""),
                        "digest": d})
    return {"analysis": analysis, "chapter_digests": digests}


@router.put("/analysis")
def update_analysis(pid: str, body: AnalysisUpdate) -> dict:
    with get_pool().connection() as c:
        c.execute(
            "UPDATE projects SET analysis=%s WHERE id=%s",
            (Json(body.analysis), pid),
        )
    return {"ok": True}


@router.put("/chapter-digests/{ci}")
def update_chapter_digest(pid: str, ci: int, body: dict) -> dict:
    storage = PostgresStorage(pid, get_pool())
    try:
        ch = storage.load_chapter(ci)
    except KeyError:
        raise HTTPException(404, "chapter not found") from None
    meta = dict(ch.meta or {})
    meta["source_digest"] = body.get("digest", "")
    ch.meta = meta
    storage.save_chapter(ch)
    return {"ok": True}
