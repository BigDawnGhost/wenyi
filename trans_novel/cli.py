"""命令行入口（Typer + Rich）。

``translate`` 保持一键完整流程并天然支持断点续跑；``prepare``、``review``、
``qa``、``report`` 与 ``assemble`` 提供可单独执行的阶段入口。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import Any, Optional, Protocol

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from typer.core import TyperGroup

from .config import Config
from .ingest.errors import IngestError
from .ingest.segmenter import load_document
from .pipeline.runstore import STATUS_DONE, RunStore, slugify


def _configure_windows_console(
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
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_windows_console()

_CONFIG = {"path": "config.yaml"}


def _config_path_from_args(args: Sequence[str]) -> str:
    """在 Click 解析参数前取得全局配置路径，确保帮助等早退命令也会初始化。"""
    for index, arg in enumerate(args):
        if arg in {"--config", "-c"}:
            if index + 1 < len(args):
                return args[index + 1]
            break
        if arg.startswith("--config="):
            return arg.partition("=")[2]
        if arg.startswith("-c") and len(arg) > 2:
            return arg[2:]
    return "config.yaml"


class _ConfigInitializingGroup(TyperGroup):
    """所有 CLI 调用在 Click 分派或早退前都检查默认配置。"""

    def main(
        self,
        args: Sequence[str] | None = None,
        *main_args: Any,
        **main_kwargs: Any,
    ) -> Any:
        """在 Click 解析命令前定位并创建缺失的默认配置文件。"""
        cli_args = list(args) if args is not None else sys.argv[1:]
        config_path = _config_path_from_args(cli_args)
        _CONFIG["path"] = config_path
        Config.create_default_file(config_path)
        return super().main(args=args, *main_args, **main_kwargs)


app = typer.Typer(
    cls=_ConfigInitializingGroup,
    add_completion=False,
    no_args_is_help=True,
    help="面向长篇小说的多语言翻译工作流。",
)
glossary_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="查看术语、检查冲突并裁定固定译名。",
)
console = Console()


class _ManifestStore(Protocol):
    def load_manifest(self) -> dict[str, Any]:
        """返回运行目录中的 manifest 数据。"""
        ...


@app.callback()
def _root(
    config: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="配置文件路径；文件不存在时自动创建",
    ),
):
    """记录本次 CLI 调用使用的全局配置文件路径。"""
    _CONFIG["path"] = config


def _load_config() -> Config:
    """加载当前 CLI 调用选定的配置文件。"""
    return Config.load(_CONFIG["path"])


def _require_input_file(input_path: str) -> None:
    """确认输入路径是文件，否则打印错误并以状态码 1 退出。"""
    if not os.path.isfile(input_path):
        console.print(f"[red]输入文件不存在：{input_path}[/]")
        raise typer.Exit(1)


def _validate_output_format(fmt: str) -> str:
    """规范化并校验用户可选择的输出格式。"""
    normalized = fmt.strip().lower()
    allowed = {"epub", "txt", "html", "markdown"}
    if normalized not in allowed:
        console.print(
            "[red]不支持的输出格式："
            f"{fmt}（可选 epub / txt / html / markdown）[/]"
        )
        raise typer.Exit(2)
    return normalized


def _runstore_for(config: Config, input_path: str) -> RunStore:
    """解析输入书名并定位其已有状态目录，不主动创建目录。"""
    _require_input_file(input_path)
    if os.path.splitext(input_path)[1].lower() == ".pdf":
        title = os.path.splitext(os.path.basename(input_path))[0]
        run_dir = os.path.join(config.state_dir, slugify(title))
        return RunStore(run_dir, create=False)
    doc = load_document(input_path, config.source_lang, config.target_lang)
    run_dir = os.path.join(config.state_dir, slugify(doc.title))
    return RunStore(run_dir, create=False)


def _apply_store_languages(config: Config, store: _ManifestStore) -> None:
    """独立阶段命令从运行 manifest 恢复实际源语言和目标语言。"""
    manifest = store.load_manifest()
    source_lang = manifest.get("source_lang")
    target_lang = manifest.get("target_lang")
    if isinstance(source_lang, str) and source_lang:
        config.source_lang = source_lang
    if isinstance(target_lang, str) and target_lang:
        config.target_lang = target_lang


def _translate_impl(
    input_path: str,
    *,
    chapter: Optional[int] = None,
    fmt: str = "epub",
    out: Optional[str] = None,
    polish: Optional[bool] = None,
    review: Optional[bool] = None,
    qa: Optional[bool] = None,
    mono: Optional[bool] = None,
    bilingual: Optional[bool] = None,
) -> None:
    """执行一键翻译流程，并把预期的输入/配置错误转成简洁 CLI 提示。"""
    try:
        _translate_impl_or_raise(
            input_path,
            chapter=chapter,
            fmt=fmt,
            out=out,
            polish=polish,
            review=review,
            qa=qa,
            mono=mono,
            bilingual=bilingual,
        )
    except (IngestError, ImportError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None


def _translate_impl_or_raise(
    input_path: str,
    *,
    chapter: Optional[int] = None,
    fmt: str = "epub",
    out: Optional[str] = None,
    polish: Optional[bool] = None,
    review: Optional[bool] = None,
    qa: Optional[bool] = None,
    mono: Optional[bool] = None,
    bilingual: Optional[bool] = None,
) -> None:
    """执行翻译并保留原异常，由 ``_translate_impl`` 转为 CLI 错误。"""
    from .pipeline.orchestrator import Orchestrator

    _require_input_file(input_path)
    fmt = _validate_output_format(fmt)
    config = _load_config()
    if polish is not None:
        config.pipeline.polish = polish
    if review is not None:
        config.pipeline.review = review
    if mono is not None:
        config.output.mono = mono
    if bilingual is not None:
        config.output.bilingual = bilingual
    if chapter is not None:
        ignored: list[str] = []
        if fmt != "epub":
            ignored.append("--format")
        if out is not None:
            ignored.append("--out")
        if review is not None:
            ignored.append("--review/--no-review")
        if qa is not None:
            ignored.append("--qa/--no-qa")
        if mono is not None:
            ignored.append("--mono/--no-mono")
        if bilingual is not None:
            ignored.append("--bilingual/--no-bilingual")
        if ignored:
            raise ValueError(
                "--chapter 只翻译并保存指定章节，不能同时使用收尾选项："
                + "、".join(ignored)
            )

    orch = Orchestrator(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("准备中…", total=None)

        def cb(done: int, total: int, label: str) -> None:
            """把编排器的通用进度回调同步到 Rich 任务。"""
            nonlocal task
            if total > 0:
                prog.update(task, completed=done, total=total, description=label)
                return
            # Rich 的 update(total=None) 表示“不修改 total”，无法从上一阶段的
            # 确定总数切回滚动模式；重建任务以清除残留的章节/段落计数。
            prog.remove_task(task)
            task = prog.add_task(label, total=None)

        if chapter is not None:
            try:
                store = orch.run(input_path, only_chapter=chapter, progress=cb)
            except ValueError as error:
                console.print(f"[red]{error}[/]")
                raise typer.Exit(2) from error
            console.print(f"[green]已翻第 {chapter} 章[/]，状态目录：{store.run_dir}")
            _print_usage({"usage": store.load_usage() or {}})
            return

        result = orch.run_all(
            input_path,
            progress=cb,
            out_format=fmt,
            out_path=out,
            do_qa=qa,
        )

    s = result["report"]["summary"]
    console.print(
        f"[bold green]完成[/]：{s['chapters_done']}/{s['chapters_total']} 章，"
        f"审校 {s.get('chapters_reviewed', 0)}/{s['chapters_total']} 章，"
        f"术语 {s['terms']}，一致性问题 {len(result['qa_issues'])} 项。"
    )
    _print_usage({"usage": result["store"].load_usage() or {}})
    for path in result.get("outputs") or [result["output"]]:
        console.print(f"译文：[bold]{path}[/]")


def _prepare_impl(input_path: str) -> None:
    """完成译前准备并停止，不生成正文译文或输出文件。"""
    from .pipeline.orchestrator import Orchestrator

    try:
        _require_input_file(input_path)
        config = _load_config()
        orch = Orchestrator(config)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as prog:
            task = prog.add_task("准备中…", total=None)

            def cb(done: int, total: int, label: str) -> None:
                """把译前准备进度同步到 Rich 任务。"""
                nonlocal task
                if total > 0:
                    prog.update(task, completed=done, total=total, description=label)
                    return
                prog.remove_task(task)
                task = prog.add_task(label, total=None)

            store = orch.prepare_for_translation(input_path, progress=cb)
    except (IngestError, ImportError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None

    manifest = store.load_manifest()
    chapters = manifest.get("chapters", [])
    analysis = store.load_analysis() or {}
    digests = sum(
        bool(store.load_chapter(item["index"]).meta.get("source_digest"))
        for item in chapters
    )
    console.print(
        f"[bold green]准备完成[/]：解析 {len(chapters)} 章，"
        f"预扫 {digests}/{len(chapters)} 章，"
        f"全书概览{' 已生成' if analysis.get('book_synopsis') else ' 未生成'}。"
    )
    console.print(f"状态目录：[bold]{store.run_dir}[/]")
    console.print("运行 translate 并传入同一源文件即可继续完成全书翻译。")
    _print_usage({"usage": store.load_usage() or {}})


def _print_usage(report: dict) -> None:
    """打印本书累计 token 用量与分档缓存命中率（无数据时静默跳过）。"""
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
    for tier, v in sorted(usage.get("by_tier", {}).items()):
        console.print(
            f"  · {tier}：{v['total_tokens']:,} tok，{v['calls']} 次调用，"
            f"缓存命中率 {v['cache_hit_rate']:.1%}"
        )
    for stage, v in sorted(
        (usage.get("by_stage") or {}).items(),
        key=lambda item: -item[1]["total_tokens"],
    ):
        console.print(
            f"  · 阶段 {stage}：{v['total_tokens']:,} tok"
            f"（提示 {v['prompt_tokens']:,} / 生成 {v['completion_tokens']:,}），"
            f"{v['calls']} 次调用，缓存命中率 {v['cache_hit_rate']:.1%}"
        )


# ── 一键完整流程 / 译前准备 ────────────────────────────────────────────────
@app.command(rich_help_panel="主要流程")
def translate(
    input: str = typer.Argument(
        ...,
        help="待翻译书籍（EPUB / FB2 / TXT / Markdown / HTML / PDF）",
    ),
    chapter: Optional[int] = typer.Option(
        None,
        "--chapter",
        min=0,
        help="仅翻译并保存指定章节（从 0 起）；不执行审校、QA、报告和导出",
    ),
    fmt: str = typer.Option(
        "epub",
        "--format",
        help="最终导出格式：epub / txt / html / markdown",
    ),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help="单语版输出路径；默认写入源文件旁的 output 目录",
    ),
    polish: Optional[bool] = typer.Option(
        None,
        "--polish/--no-polish",
        help="覆盖 pipeline.polish，控制翻译后是否润色",
    ),
    review: Optional[bool] = typer.Option(
        None,
        "--review/--no-review",
        help="覆盖 pipeline.review，控制全书翻译后是否执行最终审校",
    ),
    qa: Optional[bool] = typer.Option(
        None,
        "--qa/--no-qa",
        help="覆盖 pipeline.consistency_qa，控制是否执行跨章一致性扫描",
    ),
    mono: Optional[bool] = typer.Option(
        None,
        "--mono/--no-mono",
        help="覆盖 output.mono，控制是否生成单语版",
    ),
    bilingual: Optional[bool] = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help="覆盖 output.bilingual，控制是否生成原文译文对照版",
    ),
):
    """一键完成准备、翻译、可选审校/QA、报告和导出；中断后原命令续跑。"""
    _translate_impl(
        input,
        chapter=chapter,
        fmt=fmt,
        out=out,
        polish=polish,
        review=review,
        qa=qa,
        mono=mono,
        bilingual=bilingual,
    )


@app.command(rich_help_panel="主要流程")
def prepare(
    input: str = typer.Argument(
        ...,
        help="待准备书籍（EPUB / FB2 / TXT / Markdown / HTML / PDF）",
    ),
) -> None:
    """只解析书籍、识别语言、分析风格和术语并预扫全书，不翻译正文。"""
    _prepare_impl(input)


@app.command(rich_help_panel="质量检查")
def review(
    input: str = typer.Argument(..., help="全书正文已经翻译完成的源文件"),
    force: bool = typer.Option(
        False, "--force", help="忽略审校摘要，强制重新审校全部章节"
    ),
    fix: Optional[bool] = typer.Option(
        None,
        "--fix/--no-fix",
        help="覆盖 pipeline.autofix_severe；开启后串行修复漏译和误译",
    ),
):
    """使用最终术语库审校完整译文；结果按章保存，可断点续审。"""
    from .pipeline.orchestrator import Orchestrator

    _require_input_file(input)
    config = _load_config()
    autofix = config.pipeline.autofix_severe if fix is None else fix
    orch = Orchestrator(config)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as prog:
            task = prog.add_task("准备全书审校…", total=None)

            def cb(done: int, total: int, label: str) -> None:
                """把全书审校进度同步到 Rich 任务。"""
                nonlocal task
                if total > 0:
                    prog.update(
                        task,
                        completed=done,
                        total=total,
                        description=label,
                    )
                    return
                prog.remove_task(task)
                task = prog.add_task(label, total=None)

            result = orch.run_review(
                input,
                progress=cb,
                force=force,
                autofix=autofix,
            )
    except (IngestError, ImportError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None

    issues = result["review_issues"]
    console.print(
        f"[bold green]全书审校完成[/]：发现 {len(issues)} 项问题"
        f"{'，已按配置尝试修复严重项' if autofix else ''}。"
    )
    console.print(f"状态目录：{result['store'].run_dir}")
    _print_usage({"usage": result["store"].load_usage() or {}})


# ── 查询 / 细粒度命令 ──────────────────────────────────────────────────────
@app.command(rich_help_panel="状态与输出")
def status(
    input: str = typer.Argument(..., help="已建立翻译状态的源文件"),
) -> None:
    """查看各章进度与术语库统计。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 prepare 或 translate。[/]")
        raise typer.Exit(1)
    m = store.load_manifest()
    console.print(
        f"《{m['title']}》（{m['fmt']}）  {m['source_lang']}→{m['target_lang']}"
    )
    table = Table("", "#", "章节", "翻译", "审校")
    for c in m["chapters"]:
        mark = "✓" if c["status"] == STATUS_DONE else "·"
        table.add_row(
            mark,
            str(c["index"]),
            c["title"],
            c["status"],
            str(c.get("review_status", "pending")),
        )
    console.print(table)
    g = GlossaryStore(store.glossary_path)
    console.print("术语库：", g.stats())
    g.close()


@glossary_app.command("list")
def glossary_list(
    input: str = typer.Argument(..., help="已建立翻译状态的源文件"),
) -> None:
    """列出当前书籍术语库中的固定译名和状态。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 prepare 或 translate。[/]")
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    try:
        table = Table("原文", "译文", "类型", "状态")
        for term in g.all_terms():
            table.add_row(
                term.source,
                term.target,
                f"{term.type}{'/' + term.gender if term.gender else ''}",
                term.status,
            )
        console.print(table)
    finally:
        g.close()


@glossary_app.command("conflicts")
def glossary_conflicts(
    input: str = typer.Argument(..., help="已建立翻译状态的源文件"),
) -> None:
    """列出模型抽取过程中发现的未裁定译名冲突。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate 或 prepare。[/]")
        raise typer.Exit(1)
    glossary = GlossaryStore(store.glossary_path)
    try:
        conflicts = glossary.open_conflicts()
        if not conflicts:
            console.print("没有待裁定的术语冲突。")
            return
        for conflict in conflicts:
            console.print(
                f"  {conflict['source']}: 现有「{conflict['existing_target']}」 vs "
                f"提议「{conflict['proposed_target']}」"
                f"（第 {conflict['chapter']} 章）"
            )
    finally:
        glossary.close()


@glossary_app.command("resolve")
def glossary_resolve(
    input: str = typer.Argument(..., help="已建立翻译状态的源文件"),
    source: str = typer.Argument(..., help="需要裁定的原文术语"),
    target: str = typer.Argument(..., help="今后统一采用的目标语言译名"),
) -> None:
    """把一个已有术语裁定为指定译名，并关闭对应冲突。"""
    from .glossary import resolver
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate 或 prepare。[/]")
        raise typer.Exit(1)
    glossary = GlossaryStore(store.glossary_path)
    try:
        if not resolver.resolve(glossary, source, target):
            console.print(f"[red]术语不存在：{source}[/]")
            raise typer.Exit(1)
        console.print(f"已裁定 {source} → {target}")
    finally:
        glossary.close()


@app.command(rich_help_panel="状态与输出")
def assemble(
    input: str = typer.Argument(..., help="已完成或部分完成翻译的源文件"),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help="单语版输出路径；默认写入源文件旁的 output 目录",
    ),
    fmt: str = typer.Option(
        "epub",
        "--format",
        help="导出格式：epub / txt / html / markdown",
    ),
    mono: Optional[bool] = typer.Option(
        None,
        "--mono/--no-mono",
        help="覆盖 output.mono，控制是否生成单语版",
    ),
    bilingual: Optional[bool] = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help="覆盖 output.bilingual，控制是否生成原文译文对照版",
    ),
):
    """从已有状态重新生成译文文件，不调用模型或重新翻译。"""
    from .assemble.writer import assemble as do_assemble
    from .assemble.writer import bilingual_out_path

    config = _load_config()
    fmt = _validate_output_format(fmt)
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 prepare 或 translate。[/]")
        raise typer.Exit(1)
    do_mono = config.output.mono if mono is None else mono
    do_bilingual = config.output.bilingual if bilingual is None else bilingual
    if not do_mono and not do_bilingual:
        do_mono = True  # 兜底：至少产一个单语产物
    paths: list[str] = []
    if do_mono:
        paths.append(
            do_assemble(
                store,
                input,
                out_path=out,
                out_format=fmt,
                bilingual=False,
                about_page=config.output.about_page,
            )
        )
    if do_bilingual:
        bi_out = bilingual_out_path(out) if out else None
        paths.append(
            do_assemble(
                store,
                input,
                out_path=bi_out,
                out_format=fmt,
                bilingual=True,
                order=config.output.bilingual_order,
                preserve_source_style=(
                    config.output.bilingual_preserve_source_style
                ),
                about_page=config.output.about_page,
            )
        )
    for path in paths:
        console.print(f"已生成译文：[bold]{path}[/]")


@app.command(rich_help_panel="质量检查")
def qa(
    input: str = typer.Argument(..., help="已完成翻译的源文件"),
) -> None:
    """调用模型执行全书跨章一致性扫描，只报告问题而不修改正文。"""
    from .agents.consistency import ConsistencyChecker
    from .glossary.store import GlossaryStore
    from .llm.factory import build_client

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 prepare 或 translate。[/]")
        raise typer.Exit(1)
    _apply_store_languages(config, store)
    g = GlossaryStore(store.glossary_path)
    try:
        issues = ConsistencyChecker(build_client(config), config).check(store, g)
    finally:
        g.close()
    console.print(f"一致性问题 {len(issues)} 项：")
    for it in issues:
        console.print(
            f"  [{it.get('type')}] {it.get('detail')}  ({it.get('where', '')})"
        )


@app.command(rich_help_panel="状态与输出")
def report(
    input: str = typer.Argument(..., help="已建立翻译状态的源文件"),
) -> None:
    """根据当前章节、审校和术语状态重新生成 report.json，不调用模型。"""
    from .assemble.report import build_report
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 prepare 或 translate。[/]")
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    rep = build_report(store, g)
    g.close()
    store.save_report(rep)
    s = rep["summary"]
    console.print(f"QA 报告已写入 {store.report_path}")
    console.print(
        f"  章节 {s['chapters_done']}/{s['chapters_total']}  术语 {s['terms']}  "
        f"待裁决冲突 {s['open_conflicts']}  审校问题 {s['review_issues']}  "
        f"回译疑点 {s['backtranslation_issues']}"
    )


app.add_typer(glossary_app, name="glossary", rich_help_panel="术语库")


def main() -> None:
    """启动 Typer 命令行应用。"""
    app()


if __name__ == "__main__":
    main()
