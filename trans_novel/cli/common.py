"""Shared state and rendering helpers for the command-line applications."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import typer
import yaml
from rich.console import Console

from trans_novel.config import Config
from trans_novel.pipeline.state import RunStore
from trans_novel.pipeline.state import runstore_for as resolve_runstore

console = Console()
_CONFIG = {"path": "config.yaml"}


def configure_windows_console(
    streams: tuple[object, ...] | None = None,
    *,
    is_windows: bool | None = None,
) -> None:
    """让 Windows 控制台能输出中文；PyInstaller 单文件启动时尤其需要。"""
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return
    for stream in streams or (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


configure_windows_console()


def set_config_path(path: str) -> None:
    _CONFIG["path"] = path


def config_path() -> Path:
    return Path(_CONFIG["path"]).expanduser()


def load_config() -> Config:
    path = Path(_CONFIG["path"]).expanduser()
    try:
        return Config.load(str(path))
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError) as error:
        console.print(f"[red]配置文件无效：{path}[/]")
        console.print(str(error))
        raise typer.Exit(2) from None


def require_input_file(input_path: str) -> None:
    if not os.path.isfile(input_path):
        console.print(f"[red]输入文件不存在：{input_path}[/]")
        raise typer.Exit(1)


def validate_output_format(fmt: str) -> str:
    normalized = fmt.strip().lower()
    if normalized not in {"epub", "txt"}:
        console.print(f"[red]不支持的输出格式：{fmt}（可选 epub / txt）[/]")
        raise typer.Exit(2)
    return normalized


def runstore_for(config: Config, input_path: str) -> RunStore:
    require_input_file(input_path)
    return resolve_runstore(config, input_path)


def print_chapter_processing(report: dict) -> None:
    processing = report.get("chapter_processing")
    if isinstance(processing, dict):
        preserved = processing.get("preserved") or []
        review_required = processing.get("review_required") or []
        if preserved:
            console.print("[yellow]以下章节按原文保留：[/]")
            for chapter in preserved:
                console.print(
                    f"  第{chapter['chapter']}章 {chapter['title']} —— {chapter['reason']}"
                )
        if review_required:
            console.print("[yellow]以下章节已翻译，但建议人工复核：[/]")
            for chapter in review_required:
                console.print(
                    f"  第{chapter['chapter']}章 {chapter['title']} —— {chapter['reason']}"
                )
        return
    back_matter = report.get("back_matter_chapters") or []
    if back_matter:
        console.print("[yellow]历史运行中的附属章节：[/]")
        for chapter in back_matter:
            console.print(f"  第{chapter['chapter']}章 {chapter['title']} —— {chapter['mode']}")


def print_theme_warnings(report: dict | None) -> None:
    """显示整本书未匹配到任何排版角色的主题警告。"""
    theme = report.get("theme") if isinstance(report, dict) else None
    if isinstance(theme, dict) and theme.get("warning_counts", {}).get("zero_role_coverage"):
        console.print("[yellow]EPUB 主题未匹配任何排版角色；请检查分类规则。[/]")


def print_usage(report: dict) -> None:
    """打印本书累计 token 用量与分 Agent 缓存命中率（无数据时静默跳过）。"""
    usage = report.get("usage") or {}
    totals = usage.get("totals") or {}
    if not totals.get("total_tokens"):
        return
    console.print(
        f"用量（本书累计）：{totals['total_tokens']:,} tok"
        f"（提示 {totals['prompt_tokens']:,} / 生成 {totals['completion_tokens']:,}），"
        f"缓存命中率 {totals.get('cache_hit_rate', 0.0):.1%}"
        f"（命中 {totals['cache_hit_tokens']:,} / 未命中 {totals['cache_miss_tokens']:,} tok）"
    )
    for agent, value in sorted(
        usage.get("by_agent", {}).items(), key=lambda item: -item[1]["total_tokens"]
    ):
        console.print(
            f"  · {agent}：{value['total_tokens']:,} tok，{value['calls']} 次调用，"
            f"缓存命中率 {value['cache_hit_rate']:.1%}"
        )
    stages = usage.get("by_stage") or {}
    for stage, value in sorted(stages.items(), key=lambda item: -item[1]["total_tokens"]):
        console.print(
            f"  · 阶段 {stage}：{value['total_tokens']:,} tok"
            f"（提示 {value['prompt_tokens']:,} / 生成 {value['completion_tokens']:,}），"
            f"{value['calls']} 次调用，缓存命中率 {value['cache_hit_rate']:.1%}"
        )


__all__ = [
    "config_path",
    "configure_windows_console",
    "console",
    "load_config",
    "print_chapter_processing",
    "print_theme_warnings",
    "print_usage",
    "require_input_file",
    "runstore_for",
    "set_config_path",
    "validate_output_format",
]
