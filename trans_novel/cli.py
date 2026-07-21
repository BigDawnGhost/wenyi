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
import typer.rich_utils as typer_rich_utils
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

from . import languages as language
from .config import Config
from .ingest.errors import IngestError
from .ingest.segmenter import load_document
from .locales import message as _
from .pipeline.runstore import STATUS_DONE, RunStore, run_slug

typer_rich_utils.OPTIONS_PANEL_TITLE = _("panel.options")
typer_rich_utils.ARGUMENTS_PANEL_TITLE = _("panel.arguments")
typer_rich_utils.COMMANDS_PANEL_TITLE = _("panel.commands")


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
    help=_("app.help"),
)
glossary_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=_("glossary.help"),
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
        help=_("option.config"),
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
        console.print(f"[red]{_('error.input_missing', path=input_path)}[/]")
        raise typer.Exit(1)


def _validate_output_format(fmt: str) -> str:
    """规范化并校验用户可选择的输出格式。"""
    normalized = fmt.strip().lower()
    allowed = {"epub", "txt", "html", "markdown"}
    if normalized not in allowed:
        console.print(f"[red]{_('error.unsupported_format', format=fmt)}[/]")
        raise typer.Exit(2)
    return normalized


def _runstore_for(config: Config, input_path: str) -> RunStore:
    """解析输入书名并定位已有状态，存在时校验并恢复语言对。"""
    _require_input_file(input_path)
    if os.path.splitext(input_path)[1].lower() == ".pdf":
        title = os.path.splitext(os.path.basename(input_path))[0]
        run_dir = os.path.join(
            config.state_dir,
            run_slug(title, config.target_lang),
        )
    else:
        doc = load_document(input_path, config.source_lang, config.target_lang)
        run_dir = os.path.join(
            config.state_dir,
            run_slug(doc.title, config.target_lang),
        )
    store = RunStore(run_dir, create=False)
    if store.exists():
        _apply_store_languages(config, store)
    return store


def _apply_store_languages(config: Config, store: _ManifestStore) -> None:
    """独立阶段命令校验 manifest，并恢复实际源语言和目标语言。"""
    manifest = store.load_manifest()
    source_lang = manifest.get("source_lang")
    target_lang = manifest.get("target_lang")
    if not isinstance(source_lang, str) or not source_lang.strip():
        raise ValueError(_("error.manifest_source_missing"))
    if not isinstance(target_lang, str) or not target_lang.strip():
        raise ValueError(_("error.manifest_target_missing"))

    source = language.canonical_tag(source_lang)
    target = language.canonical_tag(target_lang)
    requested_source = language.canonical_tag(config.source_lang)
    requested_target = language.canonical_tag(config.target_lang)
    if requested_source not in {"", "auto"} and requested_source != source:
        raise ValueError(
            _(
                "error.source_mismatch",
                configured=requested_source,
                stored=source,
            )
        )
    if (
        requested_target
        and language.target_identity(requested_target)
        != language.target_identity(target)
    ):
        raise ValueError(
            _(
                "error.target_mismatch",
                configured=requested_target,
                stored=target,
            )
        )
    config.source_lang = source
    config.target_lang = target


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
        console.print(f"[red]{_('error.prefix', error=error)}[/]")
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
                _(
                    "error.chapter_finish_options",
                    options=", ".join(ignored),
                )
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
        task = prog.add_task(_("progress.preparing"), total=None)

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
                console.print(f"[red]{_('error.prefix', error=error)}[/]")
                raise typer.Exit(2) from error
            console.print(
                _(
                    "result.chapter_done",
                    chapter=chapter,
                    path=store.run_dir,
                )
            )
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
        _(
            "result.translation_complete",
            done=s["chapters_done"],
            total=s["chapters_total"],
            reviewed=s.get("chapters_reviewed", 0),
            terms=s["terms"],
            issues=len(result["qa_issues"]),
        )
    )
    _print_usage({"usage": result["store"].load_usage() or {}})
    for path in result.get("outputs") or [result["output"]]:
        console.print(_("result.translation_output", path=path))


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
            task = prog.add_task(_("progress.preparing"), total=None)

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
        console.print(f"[red]{_('error.prefix', error=error)}[/]")
        raise typer.Exit(1) from None

    manifest = store.load_manifest()
    chapters = manifest.get("chapters", [])
    analysis = store.load_analysis() or {}
    digests = sum(
        bool(store.load_chapter(item["index"]).meta.get("source_digest"))
        for item in chapters
    )
    console.print(
        _(
            "result.prepare_complete",
            chapters=len(chapters),
            digests=digests,
            overview=_(
                "result.overview_generated"
                if analysis.get("book_synopsis")
                else "result.overview_missing"
            ),
        )
    )
    console.print(_("result.state_directory", path=store.run_dir))
    console.print(_("result.prepare_resume"))
    _print_usage({"usage": store.load_usage() or {}})


def _print_usage(report: dict) -> None:
    """打印本书累计 token 用量与分档缓存命中率（无数据时静默跳过）。"""
    usage = report.get("usage") or {}
    totals = usage.get("totals") or {}
    if not totals.get("total_tokens"):
        return
    console.print(
        _(
            "usage.total",
            total=totals["total_tokens"],
            prompt=totals["prompt_tokens"],
            completion=totals["completion_tokens"],
            rate=totals.get("cache_hit_rate", 0.0),
            hit=totals["cache_hit_tokens"],
            miss=totals["cache_miss_tokens"],
        )
    )
    for tier, v in sorted(usage.get("by_tier", {}).items()):
        console.print(
            _(
                "usage.tier",
                tier=tier,
                total=v["total_tokens"],
                calls=v["calls"],
                rate=v["cache_hit_rate"],
            )
        )
    for stage, v in sorted(
        (usage.get("by_stage") or {}).items(),
        key=lambda item: -item[1]["total_tokens"],
    ):
        console.print(
            _(
                "usage.stage",
                stage=stage,
                total=v["total_tokens"],
                prompt=v["prompt_tokens"],
                completion=v["completion_tokens"],
                calls=v["calls"],
                rate=v["cache_hit_rate"],
            )
        )


# ── 一键完整流程 / 译前准备 ────────────────────────────────────────────────
@app.command(help=_("command.translate"), rich_help_panel=_("panel.workflow"))
def translate(
    input: str = typer.Argument(
        ...,
        help=_("option.translate.input"),
    ),
    chapter: Optional[int] = typer.Option(
        None,
        "--chapter",
        min=0,
        help=_("option.translate.chapter"),
    ),
    fmt: str = typer.Option(
        "epub",
        "--format",
        help=_("option.translate.format"),
    ),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help=_("option.output"),
    ),
    polish: Optional[bool] = typer.Option(
        None,
        "--polish/--no-polish",
        help=_("option.polish"),
    ),
    review: Optional[bool] = typer.Option(
        None,
        "--review/--no-review",
        help=_("option.review"),
    ),
    qa: Optional[bool] = typer.Option(
        None,
        "--qa/--no-qa",
        help=_("option.qa"),
    ),
    mono: Optional[bool] = typer.Option(
        None,
        "--mono/--no-mono",
        help=_("option.mono"),
    ),
    bilingual: Optional[bool] = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help=_("option.bilingual"),
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


@app.command(help=_("command.prepare"), rich_help_panel=_("panel.workflow"))
def prepare(
    input: str = typer.Argument(
        ...,
        help=_("option.prepare.input"),
    ),
) -> None:
    """只解析书籍、识别语言、分析风格和术语并预扫全书，不翻译正文。"""
    _prepare_impl(input)


@app.command(help=_("command.review"), rich_help_panel=_("panel.quality"))
def review(
    input: str = typer.Argument(..., help=_("option.review.input")),
    force: bool = typer.Option(
        False,
        "--force",
        help=_("option.review.force"),
    ),
    fix: Optional[bool] = typer.Option(
        None,
        "--fix/--no-fix",
        help=_("option.review.fix"),
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
            task = prog.add_task(_("progress.review_preparing"), total=None)

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
        console.print(f"[red]{_('error.prefix', error=error)}[/]")
        raise typer.Exit(1) from None

    issues = result["review_issues"]
    console.print(
        _(
            "result.review_complete",
            issues=len(issues),
            fixed=_("result.review_fixed") if autofix else "",
        )
    )
    console.print(_("result.state_directory", path=result["store"].run_dir))
    _print_usage({"usage": result["store"].load_usage() or {}})


# ── 查询 / 细粒度命令 ──────────────────────────────────────────────────────
@app.command(help=_("command.status"), rich_help_panel=_("panel.output"))
def status(
    input: str = typer.Argument(..., help=_("option.state.input")),
) -> None:
    """查看各章进度与术语库统计。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print(_("result.no_progress"))
        raise typer.Exit(1)
    m = store.load_manifest()
    console.print(
        _(
            "status.book",
            title=m["title"],
            format=m["fmt"],
            source=m["source_lang"],
            target=m["target_lang"],
        )
    )
    table = Table(
        "",
        "#",
        _("status.chapter"),
        _("status.translation"),
        _("status.review"),
    )
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
    console.print(_("status.glossary"), g.stats())
    g.close()


@glossary_app.command("list", help=_("command.glossary.list"))
def glossary_list(
    input: str = typer.Argument(..., help=_("option.state.input")),
) -> None:
    """列出当前书籍术语库中的固定译名和状态。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print(_("result.no_progress"))
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    try:
        table = Table(
            _("glossary.source"),
            _("glossary.target"),
            _("glossary.type"),
            _("glossary.status"),
        )
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


@glossary_app.command("conflicts", help=_("command.glossary.conflicts"))
def glossary_conflicts(
    input: str = typer.Argument(..., help=_("option.state.input")),
) -> None:
    """列出模型抽取过程中发现的未裁定译名冲突。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print(_("result.no_progress"))
        raise typer.Exit(1)
    glossary = GlossaryStore(store.glossary_path)
    try:
        conflicts = glossary.open_conflicts()
        if not conflicts:
            console.print(_("glossary.no_conflicts"))
            return
        for conflict in conflicts:
            console.print(
                _(
                    "glossary.conflict",
                    source=conflict["source"],
                    existing=conflict["existing_target"],
                    proposed=conflict["proposed_target"],
                    chapter=conflict["chapter"],
                )
            )
    finally:
        glossary.close()


@glossary_app.command("resolve", help=_("command.glossary.resolve"))
def glossary_resolve(
    input: str = typer.Argument(..., help=_("option.state.input")),
    source: str = typer.Argument(..., help=_("option.glossary.source")),
    target: str = typer.Argument(..., help=_("option.glossary.target")),
) -> None:
    """把一个已有术语裁定为指定译名，并关闭对应冲突。"""
    from .glossary import resolver
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print(_("result.no_progress"))
        raise typer.Exit(1)
    glossary = GlossaryStore(store.glossary_path)
    try:
        if not resolver.resolve(glossary, source, target):
            console.print(f"[red]{_('glossary.not_found', source=source)}[/]")
            raise typer.Exit(1)
        console.print(_("glossary.resolved", source=source, target=target))
    finally:
        glossary.close()


@app.command(help=_("command.assemble"), rich_help_panel=_("panel.output"))
def assemble(
    input: str = typer.Argument(..., help=_("option.assemble.input")),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help=_("option.output"),
    ),
    fmt: str = typer.Option(
        "epub",
        "--format",
        help=_("option.assemble.format"),
    ),
    mono: Optional[bool] = typer.Option(
        None,
        "--mono/--no-mono",
        help=_("option.mono"),
    ),
    bilingual: Optional[bool] = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help=_("option.bilingual"),
    ),
):
    """从已有状态重新生成译文文件，不调用模型或重新翻译。"""
    from .assemble.writer import assemble as do_assemble
    from .assemble.writer import bilingual_out_path

    config = _load_config()
    fmt = _validate_output_format(fmt)
    store = _runstore_for(config, input)
    if not store.exists():
        console.print(_("result.no_progress"))
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
        console.print(_("result.assembled", path=path))


@app.command(help=_("command.qa"), rich_help_panel=_("panel.quality"))
def qa(
    input: str = typer.Argument(..., help=_("option.qa.input")),
) -> None:
    """调用模型执行全书跨章一致性扫描，只报告问题而不修改正文。"""
    from .agents.consistency import ConsistencyChecker
    from .glossary.store import GlossaryStore
    from .llm.factory import build_client

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print(_("result.no_progress"))
        raise typer.Exit(1)
    _apply_store_languages(config, store)
    g = GlossaryStore(store.glossary_path)
    try:
        issues = ConsistencyChecker(build_client(config), config).check(store, g)
    finally:
        g.close()
    console.print(_("qa.count", count=len(issues)))
    for it in issues:
        console.print(
            f"  [{it.get('type')}] {it.get('detail')}  ({it.get('where', '')})"
        )


@app.command(help=_("command.report"), rich_help_panel=_("panel.output"))
def report(
    input: str = typer.Argument(..., help=_("option.state.input")),
) -> None:
    """根据当前章节、审校和术语状态重新生成 report.json，不调用模型。"""
    from .assemble.report import build_report
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print(_("result.no_progress"))
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    rep = build_report(store, g)
    g.close()
    store.save_report(rep)
    s = rep["summary"]
    console.print(_("report.written", path=store.report_path))
    console.print(
        _(
            "report.summary",
            done=s["chapters_done"],
            total=s["chapters_total"],
            terms=s["terms"],
            conflicts=s["open_conflicts"],
            review=s["review_issues"],
            backtranslation=s["backtranslation_issues"],
        )
    )


app.add_typer(
    glossary_app,
    name="glossary",
    rich_help_panel=_("panel.glossary"),
)


def main() -> None:
    """启动 Typer 命令行应用。"""
    app()


if __name__ == "__main__":
    main()
