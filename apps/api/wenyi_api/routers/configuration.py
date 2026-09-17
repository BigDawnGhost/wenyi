"""Capabilities, project settings and explicit model tools."""

from __future__ import annotations

import importlib.util
from dataclasses import asdict

import yaml
from fastapi import APIRouter, HTTPException
from wenyi_core.i18n.languages import label, supported_languages
from wenyi_core.llm.operations import OPERATIONS, configured_operations
from wenyi_core.llm.registry import PROVIDERS
from wenyi_core.llm.router import RoutedLLMClient
from wenyi_core.llm.routing import resolve_routes

from .. import dal
from ..config_documents import project_document
from ..global_settings import load_settings, registry_guard
from ..project_service import (
    config_document,
    config_response,
    effective_config,
    parse_project_yaml,
    project_write,
    require_project,
    storage_for,
)
from ..schemas import (
    Capabilities,
    ConfigInput,
    ModelCheckRequest,
    ModelCheckResult,
    ProjectConfigOut,
    ProjectStats,
    WorkflowOut,
)

router = APIRouter(tags=["configuration"])


@router.get("/capabilities", response_model=Capabilities)
def capabilities() -> dict:
    engines = [
        name
        for name, module in (("weasyprint", "weasyprint"), ("fpdf2", "fpdf"))
        if importlib.util.find_spec(module)
    ]
    return {
        "languages": [{"code": code, "name": label(code)} for code in supported_languages()],
        "input_formats": ["epub", "fb2", "txt", "markdown", "html", "pdf", "docx", "srt"],
        "output_formats": ["epub", "txt", "html", "markdown", "pdf", "docx", "srt"],
        "pdf": {"backends": ["mineru", "babeldoc"], "engines": engines, "export_backends": engines},
        "providers": list(PROVIDERS),
        "operations": [asdict(spec) for spec in OPERATIONS.values()],
    }


@router.get("/projects/{pid}/config", response_model=ProjectConfigOut)
def get_config(pid: str) -> dict:
    project = require_project(pid)
    try:
        return config_response(project, effective_config(project))
    except (ValueError, yaml.YAMLError) as error:
        raise HTTPException(422, str(error)) from error


@router.get("/projects/{pid}/config/defaults", response_model=ProjectConfigOut)
def project_defaults(pid: str) -> dict:
    project = require_project(pid)
    try:
        return config_response(project, effective_config(project, document={}))
    except (ValueError, yaml.YAMLError) as error:
        raise HTTPException(422, str(error)) from error


@router.post("/projects/{pid}/config/validate", response_model=ProjectConfigOut)
def validate_config(pid: str, body: ConfigInput) -> dict:
    project = require_project(pid)
    try:
        return config_response(
            project, effective_config(project, document=parse_project_yaml(body.yaml))
        )
    except (ValueError, yaml.YAMLError) as error:
        raise HTTPException(422, str(error)) from error


@router.put("/projects/{pid}/config", response_model=ProjectConfigOut)
def save_config(pid: str, body: ConfigInput) -> dict:
    with project_write(pid) as (project, _storage), registry_guard() as conn:
        try:
            config = effective_config(
                project,
                document=parse_project_yaml(body.yaml),
                defaults=load_settings(connection=conn).config,
            )
        except (ValueError, yaml.YAMLError) as error:
            raise HTTPException(422, str(error)) from error
        dal.set_project_config(pid, project_document(config), connection=conn)
        # The project direction must also drive upload, list and worker dispatch.
        conn.execute(
            "UPDATE projects SET source_lang=%s, target_lang=%s WHERE id=%s",
            (config.source_lang, config.target_lang, pid),
        )
        return config_response(project, config)


@router.get("/projects/{pid}/models", response_model=list[dict])
def models(pid: str) -> list[dict]:
    return [
        route.describe()
        for route in resolve_routes(effective_config(require_project(pid)).llm).values()
    ]


@router.post("/projects/{pid}/models/check", response_model=ModelCheckResult)
def check_models(pid: str, body: ModelCheckRequest) -> dict:
    try:
        config = effective_config(require_project(pid))
        operations = configured_operations(config, body.workflow)
        RoutedLLMClient(config.llm).validate_credentials(operations)
        return {"valid": True, "operations": list(operations)}
    except (ValueError, RuntimeError) as error:
        raise HTTPException(422, str(error)) from error


@router.get("/projects/{pid}/stats", response_model=ProjectStats)
def project_stats(pid: str) -> dict:
    require_project(pid)
    store = storage_for(pid)
    from wenyi_core.llm.usage import empty_usage

    usage = store.load_usage() or empty_usage()
    timing = store.read_artifact("timing.json") or {"runs": [], "total_seconds": 0}
    return {"usage": usage, "timing": timing}


@router.get("/projects/{pid}/workflow", response_model=WorkflowOut)
def workflow(pid: str) -> dict:
    """Describe the latest workflow using its frozen configuration, not edited settings."""
    import json

    from redis import Redis

    from ..config import settings

    project = require_project(pid)
    job = next((j for j in dal.list_jobs(pid) if j["kind"] != "export"), None)
    params = (job or {}).get("params") or {}
    snapshot = params.get("config_snapshot")
    document = snapshot or config_document(effective_config(project))
    pipeline = document.get("pipeline", {})
    kind = job["kind"] if job else ("srt" if project.get("fmt") == "srt" else "translation")
    stages = []

    def add(key, label, enabled=True):
        stages.append({"id": key, "label": label, "enabled": bool(enabled)})

    if kind == "parse":
        add("parse", "解析原文与生成预览")
    elif kind == "srt":
        add("srt", "分批翻译字幕并保存检查点")
        add("assemble", "组装字幕文件")
    else:
        if kind != "review":
            add("prepare", "解析、术语与风格准备")
            add("book_understanding", "全书预理解", pipeline.get("book_understanding"))
        if kind in {"translation", "chapter_translation"}:
            add("translation", "分批翻译章节")
            add("polish", "批次内润色", pipeline.get("polish"))
            add("annotation_alignment", "逐段注释定位", pipeline.get("annotation_alignment"))
        if kind in {"translation", "review"}:
            review = kind == "review" or pipeline.get("review", False)
            autofix = params.get("autofix")
            if autofix is None:
                autofix = pipeline.get("review_autofix")
            add("review", "全书审校", review)
            add(
                "review_autofix",
                "修复审校问题并写回",
                review and autofix,
            )
            add("report", "生成报告")
    progress = None
    if job:
        try:
            with Redis.from_url(
                settings.redis_url, socket_timeout=1, socket_connect_timeout=1
            ) as redis:
                raw = redis.get(f"project:{pid}:progress")
            payload = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            candidate = json.loads(payload) if isinstance(payload, str) else None
            if (
                isinstance(candidate, dict)
                and candidate.get("run_id") == job.get("run_id")
                and candidate.get("project_id") == pid
            ):
                progress = candidate
        except Exception:
            # Progress is advisory: persisted job state remains available without Redis.
            pass
    return {
        "source": "snapshot" if snapshot else "config",
        "kind": kind,
        "status": job["status"] if job else "not_started",
        "run_id": job.get("run_id") if job else None,
        "review_id": dal.job_review_id(job["id"]) if job and job.get("id") else None,
        "stages": stages,
        "progress": progress,
    }
