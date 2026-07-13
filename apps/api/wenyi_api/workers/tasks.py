"""Arq 任务实现：run_translation / run_export。

在 worker 进程里用同步内核（经 ``asyncio.to_thread`` 包裹，避免阻塞事件循环）。
存储走 PostgresStorage；进度经 RedisEmitter → Redis Pub/Sub → WebSocket。
暂停：进度回调检测 projects.status=='paused'，抛 PauseRequested 在批次边界退出。
"""

from __future__ import annotations

import asyncio
import os

from .. import dal, paths
from ..config import settings
from ..db import get_pool, init_pool
from ..emitters import redis_progress_fn
from ..storage_pg import PostgresStorage
from ..strategies import strategy_to_config

# 这些函数由 Arq 在 worker 进程调用（非 API 进程），需自行初始化 DB 池 / Redis。


class PauseRequested(Exception):
    """用户请求暂停；在批次边界抛出，已落盘的内容不会丢失。"""


def _load_base_config() -> "object":
    from wenyi_core.config import Config
    return Config.load(settings.config_path)


def _resolve_source(pid: str) -> str:
    p = dal.get_project(pid)
    if p is None or not p.get("title"):
        # 项目可能尚未上传/解析；按 fmt 兜底
        for ext in ("epub", "txt", "fb2"):
            candidate = paths.source_path(pid, "epub" if ext == "epub" else
                                           ("fb2" if ext == "fb2" else "text"))
            if os.path.isfile(candidate):
                return candidate
        raise RuntimeError(f"项目 {pid} 找不到上传原件")
    # 已上传：source_path 相对 data_dir 存储
    rel = None
    with get_pool().connection() as c:
        row = c.execute("SELECT source_path FROM projects WHERE id=%s", (pid,)).fetchone()
    if row:
        rel = row[0]
    if rel:
        abs_path = rel if os.path.isabs(rel) else os.path.join(settings.data_dir, rel)
        if os.path.isfile(abs_path):
            return abs_path
    # 兜底：扫描项目目录
    pdir = paths.project_dir(pid)
    for fn in os.listdir(pdir):
        if fn.startswith("source."):
            return os.path.join(pdir, fn)
    raise RuntimeError(f"项目 {pid} 找不到上传原件")


def _build_config_for(pid: str):
    p = dal.get_project(pid)
    base = _load_base_config()
    strategy = (p or {}).get("strategy") or {"template": "标准翻译"}
    cfg = strategy_to_config(
        strategy, base,
        source_lang=(p or {}).get("source_lang") or "auto",
        target_lang=(p or {}).get("target_lang") or "zh",
    )
    # 输出：导出文件名控制
    cfg.output.mono = True
    cfg.output.bilingual = False
    return cfg


def _progress_with_pause(redis, pid: str):
    """构造一个检查暂停位的 ProgressFn。"""
    emitter_fn = redis_progress_fn(redis, pid)

    def fn(done: int, total: int, label: str) -> None:
        emitter_fn(done, total, label)
        # 批次边界检查暂停
        if dal.is_paused(pid):
            raise PauseRequested(pid)
    return fn


def _translate_sync(pid: str, *, do_qa: bool | None = None) -> None:
    """同步执行翻译（在 worker 的 to_thread 里调用）。"""
    from wenyi_core.llm.base import build_client
    from wenyi_core.pipeline.orchestrator import Orchestrator

    pool = init_pool(settings.psycopg_dsn)
    import redis as redis_lib
    redis = redis_lib.from_url(settings.redis_url)

    cfg = _build_config_for(pid)
    source = _resolve_source(pid)
    storage = PostgresStorage(pid, pool)
    client = build_client(cfg)
    orch = Orchestrator(cfg, client=client, storage=storage)
    progress = _progress_with_pause(redis, pid)
    try:
        orch.run_all(source, progress=progress, do_qa=do_qa)
        dal.set_project_status(pid, "done")
    except PauseRequested:
        dal.set_project_status(pid, "paused")
    except Exception as e:  # noqa: BLE001
        dal.set_project_status(pid, "error")
        storage.log_event("pipeline_error", error=str(e))
        raise


async def run_translation(ctx, *, project_id: str,
                          do_qa: bool | None = None) -> None:
    """Arq 任务：翻译全书（prepare → 翻译 → QA → 报告）。"""
    dal.set_project_status(project_id, "translating")
    await asyncio.to_thread(_translate_sync, project_id, do_qa=do_qa)


def _export_sync(pid: str, *, export_id: int, fmt: str, bilingual: bool,
                 order: str, about_page: bool) -> int:
    from wenyi_core.assemble.writer import assemble

    pool = init_pool(settings.psycopg_dsn)
    storage = PostgresStorage(pid, pool)
    source = _resolve_source(pid)
    out_dir = paths.exports_dir(pid)
    base = os.path.join(out_dir, f"export.{'epub' if fmt == 'epub' else 'txt'}")
    bi_base = os.path.join(out_dir, f"export-bi.{'epub' if fmt == 'epub' else 'txt'}")
    out_path = bi_base if bilingual else base
    result_path = assemble(
        storage, source, out_path=out_path, out_format=fmt,
        bilingual=bilingual, order=order, about_page=about_page,
    )
    size = os.path.getsize(result_path) if os.path.isfile(result_path) else None
    rel = os.path.relpath(result_path, settings.data_dir)
    dal.set_export_status(export_id, "done", path=rel, size=size)
    return export_id


async def run_export(ctx, *, project_id: str, export_id: int, fmt: str = "epub",
                     bilingual: bool = False, order: str = "target_first",
                     about_page: bool = True) -> int:
    """Arq 任务：回填导出 EPUB/TXT。"""
    try:
        return await asyncio.to_thread(
            _export_sync, project_id, export_id=export_id, fmt=fmt,
            bilingual=bilingual, order=order, about_page=about_page,
        )
    except Exception:
        dal.set_export_status(export_id, "error")
        raise
