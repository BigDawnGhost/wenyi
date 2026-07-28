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
from ..db import init_pool
from ..emitters import redis_progress_fn
from ..storage_pg import PostgresStorage
from ..strategies import strategy_to_config

# 这些函数由 Arq 在 worker 进程调用（非 API 进程），需自行初始化 DB 池 / Redis。


class PauseRequested(Exception):
    """用户请求暂停；在批次边界抛出，已落盘的内容不会丢失。"""


def _load_base_config() -> "object":
    from wenyi_core.config import Config
    return Config.load(settings.config_path)


def _pipeline_storage(pid: str, pool) -> PostgresStorage:
    return PostgresStorage(pid, pool, run_dir=paths.project_dir(pid))


def _resolve_source(pid: str) -> str:
    p = dal.get_project(pid)
    if p is None:
        raise RuntimeError(f"项目 {pid} 找不到上传原件")

    rel = p.get("source_path")
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
    from wenyi_core.llm.factory import build_client
    from wenyi_core.pipeline.orchestrator import Orchestrator

    pool = init_pool(settings.psycopg_dsn)
    import redis as redis_lib
    redis = redis_lib.from_url(settings.redis_url)

    cfg = _build_config_for(pid)
    source = _resolve_source(pid)
    storage = _pipeline_storage(pid, pool)
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
    try:
        await asyncio.to_thread(_translate_sync, project_id, do_qa=do_qa)
    except Exception:
        # _translate_sync 会记录流水线运行期异常；这里兜底处理模块导入、
        # 配置加载、存储初始化等发生在其 try 块之前的启动异常。
        dal.set_project_status(project_id, "error")
        raise


def _translate_chapter_sync(pid: str, chapter_index: int) -> None:
    """同步执行单章翻译，不运行全书审校、QA、报告或组装。"""
    from wenyi_core.llm.factory import build_client
    from wenyi_core.pipeline.orchestrator import Orchestrator

    pool = init_pool(settings.psycopg_dsn)
    import redis as redis_lib
    redis = redis_lib.from_url(settings.redis_url)

    cfg = _build_config_for(pid)
    source = _resolve_source(pid)
    storage = _pipeline_storage(pid, pool)
    client = build_client(cfg)
    orch = Orchestrator(cfg, client=client, storage=storage)
    progress = _progress_with_pause(redis, pid)
    try:
        orch.run(source, only_chapter=chapter_index, progress=progress)
        chapters = dal.chapter_summaries(pid)
        status = (
            "done"
            if chapters and all(chapter["status"] == "done" for chapter in chapters)
            else "prepared"
        )
        dal.set_project_status(pid, status)
    except PauseRequested:
        storage.set_chapter_status(chapter_index, "pending")
        dal.set_project_status(pid, "paused")
    except Exception as e:  # noqa: BLE001
        storage.set_chapter_status(chapter_index, "pending")
        dal.set_project_status(pid, "error")
        storage.log_event(
            "chapter_translation_error",
            chapter=chapter_index,
            error=str(e),
        )
        raise


async def run_chapter_translation(
    ctx,
    *,
    project_id: str,
    chapter_index: int,
) -> None:
    """Arq 任务：只翻译并保存指定章节。"""
    dal.set_project_status(project_id, "translating")
    try:
        await asyncio.to_thread(
            _translate_chapter_sync,
            project_id,
            chapter_index,
        )
    except Exception:
        # _translate_chapter_sync 负责恢复章节状态；此处覆盖其 try 块之前的启动异常。
        dal.set_chapter_status(project_id, chapter_index, "pending")
        dal.set_project_status(project_id, "error")
        raise


def _prepare_sync(pid: str) -> None:
    """同步执行译前准备（解析、语言检测、风格分析、术语提取、全书概览）。"""
    from wenyi_core.llm.factory import build_client
    from wenyi_core.pipeline.orchestrator import Orchestrator

    pool = init_pool(settings.psycopg_dsn)
    import redis as redis_lib
    redis = redis_lib.from_url(settings.redis_url)

    cfg = _build_config_for(pid)
    source = _resolve_source(pid)
    storage = _pipeline_storage(pid, pool)
    client = build_client(cfg)
    orch = Orchestrator(cfg, client=client, storage=storage)
    progress = _progress_with_pause(redis, pid)
    try:
        orch.prepare_for_translation(source, progress=progress)
        dal.set_project_status(pid, "prepared")
    except PauseRequested:
        dal.set_project_status(pid, "paused")
    except Exception as e:  # noqa: BLE001
        dal.set_project_status(pid, "error")
        storage.log_event("pipeline_error", error=str(e))
        raise


async def run_prepare(ctx, *, project_id: str) -> None:
    """Arq 任务：仅译前准备（不翻译正文）。"""
    dal.set_project_status(project_id, "preparing")
    try:
        await asyncio.to_thread(_prepare_sync, project_id)
    except Exception:
        # 兜底：模块导入 / 配置 / 存储初始化等发生在 _prepare_sync try 块之前的异常。
        dal.set_project_status(project_id, "error")
        raise


def _review_sync(pid: str, *, force: bool = False, autofix: bool = True) -> None:
    """同步执行全书 AI 审校（在 worker 的 to_thread 里调用）。"""
    from wenyi_core.llm.factory import build_client
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
        orch.run_review(source, progress=progress, force=force, autofix=autofix)
        dal.set_project_status(pid, "reviewed")
    except PauseRequested:
        dal.set_project_status(pid, "paused")
    except Exception as e:  # noqa: BLE001
        dal.set_project_status(pid, "error")
        storage.log_event("pipeline_error", error=str(e))
        raise


async def run_review(ctx, *, project_id: str,
                     force: bool = False, autofix: bool = True) -> None:
    """Arq 任务：全书 AI 审校。"""
    dal.set_project_status(project_id, "reviewing")
    try:
        await asyncio.to_thread(
            _review_sync, project_id, force=force, autofix=autofix
        )
    except Exception:
        # 兜底：模块导入 / 配置 / 存储初始化等发生在 _review_sync try 块之前的异常。
        dal.set_project_status(project_id, "error")
        raise


def _record_qa_error(
    pid: str,
    error: Exception,
    *,
    completion_status: str,
) -> None:
    """尽力持久化 QA 启动或运行错误，避免状态永久停在 running。"""
    from ..qa_state import (
        COMPLETION_STATUSES,
        read_qa_state,
        write_qa_state,
    )

    try:
        pool = init_pool(settings.psycopg_dsn)
        storage = _pipeline_storage(pid, pool)
        current_state = read_qa_state(storage.load_report() or {})
        if current_state is not None and current_state["status"] == "error":
            return
        stable_status = (
            completion_status
            if completion_status in COMPLETION_STATUSES
            else "done"
        )
        write_qa_state(
            storage,
            status="error",
            completion_status=stable_status,
            error=str(error),
        )
        storage.log_event("consistency_qa_error", error=str(error))
    except Exception:  # noqa: BLE001
        pass


def _qa_sync(pid: str, *, completion_status: str = "done") -> None:
    """同步执行跨章一致性检查并把结果写入项目报告。"""
    from wenyi_core.agents.consistency import ConsistencyChecker
    from wenyi_core.assemble.report import build_report
    from wenyi_core.llm.factory import build_client

    from ..qa_state import write_qa_state

    pool = init_pool(settings.psycopg_dsn)
    import redis as redis_lib
    redis = redis_lib.from_url(settings.redis_url)

    cfg = _build_config_for(pid)
    storage = _pipeline_storage(pid, pool)
    progress = redis_progress_fn(redis, pid, kind="qa")
    try:
        progress(0, 1, "一致性检查中…")
        issues = ConsistencyChecker(build_client(cfg), cfg).check_and_record(storage)
        report = build_report(storage, consistency_issues=issues)
        storage.save_report(report)
        write_qa_state(
            storage,
            status="completed",
            completion_status=completion_status,
        )
        progress(1, 1, f"一致性检查完成：发现 {len(issues)} 项问题")
        dal.set_project_status(pid, completion_status)
    except Exception as e:  # noqa: BLE001
        dal.set_project_status(pid, "error")
        _record_qa_error(pid, e, completion_status=completion_status)
        raise


async def run_qa(ctx, *, project_id: str,
                 completion_status: str = "done") -> None:
    """Arq 任务：执行全书跨章一致性检查。"""
    dal.set_project_status(project_id, "qa")
    try:
        await asyncio.to_thread(
            _qa_sync,
            project_id,
            completion_status=completion_status,
        )
    except Exception as e:
        dal.set_project_status(project_id, "error")
        _record_qa_error(
            project_id,
            e,
            completion_status=completion_status,
        )
        raise


def _export_sync(pid: str, *, export_id: int, fmt: str, bilingual: bool,
                 order: str, about_page: bool,
                 preserve_source_style: bool = False) -> int:
    from wenyi_core.assemble.writer import assemble

    ext_map = {"epub": "epub", "txt": "txt", "html": "html", "markdown": "md"}
    ext = ext_map.get(fmt, "txt")

    pool = init_pool(settings.psycopg_dsn)
    storage = PostgresStorage(pid, pool)
    storage.repair_resource_hrefs()
    source = _resolve_source(pid)
    out_dir = paths.exports_dir(pid)
    base = os.path.join(out_dir, f"export.{ext}")
    bi_base = os.path.join(out_dir, f"export-bi.{ext}")
    out_path = bi_base if bilingual else base
    result_path = assemble(
        storage, source, out_path=out_path, out_format=fmt,
        bilingual=bilingual, order=order, about_page=about_page,
        preserve_source_style=preserve_source_style,
    )
    size = os.path.getsize(result_path) if os.path.isfile(result_path) else None
    rel = os.path.relpath(result_path, settings.data_dir)
    dal.set_export_status(export_id, "done", path=rel, size=size)
    return export_id


async def run_export(ctx, *, project_id: str, export_id: int, fmt: str = "epub",
                     bilingual: bool = False, order: str = "target_first",
                     about_page: bool = True,
                     preserve_source_style: bool = False) -> int:
    """Arq 任务：回填导出 EPUB/TXT。"""
    try:
        return await asyncio.to_thread(
            _export_sync, project_id, export_id=export_id, fmt=fmt,
            bilingual=bilingual, order=order, about_page=about_page,
            preserve_source_style=preserve_source_style,
        )
    except Exception:
        dal.set_export_status(export_id, "error")
        raise
