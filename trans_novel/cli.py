"""命令行入口（Typer + Rich）。

``translate`` 保持一键完整流程并天然支持断点续跑；``prepare``、``review``、
``report`` 与 ``assemble`` 提供可单独执行的阶段入口。
"""

from __future__ import annotations

import math
import os
import re
import sys
import threading
from collections.abc import Sequence
from importlib.metadata import version as package_version
from typing import Any, Protocol

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
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

_CONFIG: dict[str, Any] = {"path": "config.yaml", "skip_api_check": False}
_API_CHECK_EXEMPT_COMMANDS = {
    "assemble",
    "glossary",
    "report",
    "status",
}


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
        _CONFIG["skip_api_check"] = any(arg in {"--help", "-h"} for arg in cli_args)
        Config.create_default_file(config_path)
        return super().main(*main_args, args=args, **main_kwargs)


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


_LLM_STAGE_LABELS = {
    "language_detect": "Detecting language",
    "Analyzer": "Analyzing book style",
    "Synopsizer": "Building book understanding",
    "Translator": "Translating text",
    "Polisher": "Polishing translation",
    "GlossaryExtractor": "Extracting glossary terms",
    "AnnotationAligner": "Aligning annotations",
    "BackTranslator": "Back-translating samples",
    "title_translate": "Translating chapter titles",
    "Reviewer": "Reviewing translation",
    "ReviewAgent": "Investigating review issues",
    "ReviewArbiter": "Arbitrating review conflicts",
    "ReviewFixer": "Drafting review fixes",
}


def _llm_stage_label(stage: object) -> str:
    """把内部用量归因 stage 转成短小、可读的英文业务步骤。"""
    if not isinstance(stage, str) or not stage.strip():
        return "Model request"
    normalized = stage.strip()
    mapped = _LLM_STAGE_LABELS.get(normalized)
    if mapped:
        return mapped
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", normalized)
    words = re.sub(r"[_-]+", " ", words).strip()
    return words[:1].upper() + words[1:] if words else "Model request"


class _ConditionalProgressColumn(ProgressColumn):
    """在汇总行隐藏进度专用列，使未知全程 ETA 保持紧凑。"""

    def __init__(self, inner: ProgressColumn) -> None:
        super().__init__(table_column=inner.get_table_column())
        self.inner = inner

    def render(self, task: Task):
        if not task.fields.get("show_progress", True):
            return Text("")
        return self.inner(task)


class _EstimatedRemainingColumn(ProgressColumn):
    """显示带作用域标签的紧凑 ETA；无可靠样本时明确标记为计算中。"""

    max_refresh = 0.5

    def render(self, task: Task) -> Text:
        label = str(task.fields.get("eta_label") or "阶段剩余")
        remaining = task.time_remaining
        if task.total is None or remaining is None:
            value = "计算中"
        else:
            seconds = max(0, math.ceil(remaining))
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            value = (
                f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
            )
        return Text(f"{label} {value}", style="progress.remaining")


def _new_progress() -> Progress:
    """创建所有模型命令共用的 Rich 进度展示。"""
    return Progress(
        _ConditionalProgressColumn(SpinnerColumn()),
        TextColumn("[progress.description]{task.description}"),
        _ConditionalProgressColumn(BarColumn()),
        _ConditionalProgressColumn(MofNCompleteColumn()),
        _ConditionalProgressColumn(TimeElapsedColumn()),
        _EstimatedRemainingColumn(),
        console=console,
    )


class _RichProgressBridge:
    """把流水线的阶段进度映射到同一个 Rich 任务。"""

    def __init__(
        self,
        progress: Progress,
        initial_description: str,
        *,
        overall_predictable: bool = False,
    ) -> None:
        self.progress = progress
        self._lock = threading.RLock()
        self._base_description = initial_description
        self._overall_predictable = overall_predictable
        self._nested_translation = False
        self._activity_sequence = 0
        self._activities: dict[str, dict[str, Any]] = {}
        self.task = progress.add_task(
            self._stage_description(),
            total=None,
            eta_label="阶段剩余",
            show_progress=True,
        )
        self.overall_task = progress.add_task(
            "全程",
            total=None,
            eta_label="全程剩余",
            show_progress=False,
        )

    def _task(self, task_id: int) -> Task:
        return next(task for task in self.progress.tasks if task.id == task_id)

    def _activity_status(self) -> str:
        if not self._activities:
            return "LLM: idle"
        active = list(self._activities.values())
        retrying = [item for item in active if item.get("retry")]
        primary = max(retrying or active, key=lambda item: int(item["sequence"]))
        stage = primary.get("stage")
        tier = str(primary.get("tier") or "unknown")
        same_group = [
            item
            for item in active
            if item.get("stage") == stage and str(item.get("tier") or "unknown") == tier
        ]
        label = _llm_stage_label(stage)
        retry = primary.get("retry")
        if isinstance(retry, dict):
            status = (
                f"LLM: Retrying {label} [{tier}] "
                f"{retry.get('attempt', '?')}/{retry.get('max_attempts', '?')} "
                f"in {float(retry.get('wait_seconds', 0.0)):.1f}s"
            )
        else:
            status = f"LLM: {label} [{tier}]"
        if len(same_group) > 1:
            status += f" ×{len(same_group)}"
        others = len(active) - len(same_group)
        if others:
            status += f" (+{others} other request{'s' if others != 1 else ''})"
        return status

    def _stage_description(self) -> str:
        return f"{self._base_description} · {self._activity_status()}"

    def _refresh_stage_description(self) -> None:
        self.progress.update(self.task, description=self._stage_description())

    def on_llm_activity(self, event: str, **data: Any) -> None:
        """聚合并发模型请求，并只刷新当前阶段标题。"""
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        with self._lock:
            if event == "request_started":
                self._activity_sequence += 1
                self._activities[request_id] = {
                    "stage": data.get("stage"),
                    "tier": data.get("tier"),
                    "sequence": self._activity_sequence,
                }
            elif event == "request_retry_wait":
                activity = self._activities.get(request_id)
                if activity is not None:
                    self._activity_sequence += 1
                    activity["sequence"] = self._activity_sequence
                    activity["retry"] = {
                        "attempt": data.get("attempt"),
                        "max_attempts": data.get("max_attempts"),
                        "wait_seconds": data.get("wait_seconds", 0.0),
                    }
            elif event == "request_finished":
                self._activities.pop(request_id, None)
            else:
                return
            self._refresh_stage_description()

    def close(self) -> None:
        """释放仅属于当前 Rich 上下文的瞬时请求状态。"""
        with self._lock:
            self._activities.clear()

    def _reset_stage(self, done: int, total: int, label: str) -> None:
        self._base_description = label
        self.progress.reset(
            self.task,
            total=total,
            completed=done,
            description=self._stage_description(),
            eta_label="阶段剩余",
            show_progress=True,
        )

    def update_translation(
        self,
        chapter_done: int,
        chapter_total: int,
        overall_done: int,
        overall_total: int,
        label: str,
    ) -> None:
        """同时更新当前章节和全书正文的双层进度。"""
        with self._lock:
            stage_task = self._task(self.task)
            if (
                not self._nested_translation
                or label != self._base_description
                or stage_task.total != chapter_total
                or chapter_done < stage_task.completed
            ):
                self._reset_stage(chapter_done, chapter_total, label)
            else:
                self.progress.update(
                    self.task,
                    completed=chapter_done,
                    total=chapter_total,
                    description=self._stage_description(),
                )

            overall_task = self._task(self.overall_task)
            description = "全程" if self._overall_predictable else "全程计算中（正文）"
            eta_label = "全程剩余" if self._overall_predictable else "正文剩余"
            if (
                not self._nested_translation
                or overall_task.total != overall_total
                or overall_done < overall_task.completed
            ):
                self.progress.reset(
                    self.overall_task,
                    total=overall_total,
                    completed=overall_done,
                    description=description,
                    eta_label=eta_label,
                    show_progress=True,
                )
            else:
                self.progress.update(
                    self.overall_task,
                    completed=overall_done,
                    total=overall_total,
                    description=description,
                    eta_label=eta_label,
                    show_progress=True,
                )
            self._nested_translation = True

    def __call__(self, done: int, total: int, label: str) -> None:
        """刷新当前阶段的标题与计数，避免阶段切换产生多行进度条。"""
        with self._lock:
            if self._nested_translation:
                self._nested_translation = False
            self._base_description = label
            if total > 0:
                task = self._task(self.task)
                if task.total != total or done < task.completed:
                    self._reset_stage(done, total, label)
                else:
                    self.progress.update(
                        self.task,
                        completed=done,
                        total=total,
                        description=self._stage_description(),
                    )
                return
            # Rich 的 update(total=None) 表示“不修改 total”，无法从上一阶段的
            # 确定总数切回滚动模式；重建任务以清除残留的阶段计数和测速样本。
            self.progress.remove_task(self.task)
            self.task = self.progress.add_task(
                self._stage_description(),
                total=None,
                eta_label="阶段剩余",
                show_progress=True,
            )


def _close_progress_activity(client: object, bridge: _RichProgressBridge) -> None:
    """在 Rich 上下文结束前解除客户端对桥接器的引用。"""
    set_activity_sink = getattr(client, "set_activity_sink", None)
    if callable(set_activity_sink):
        set_activity_sink(None)
    bridge.close()


def _version_callback(value: bool) -> None:
    """打印由 Git 标签生成的已安装包版本并立即退出。"""
    if value:
        console.print(package_version("trans-novel"))
        raise typer.Exit()


class _ManifestStore(Protocol):
    def load_manifest(self) -> dict[str, Any]:
        """返回运行目录中的 manifest 数据。"""
        ...


@app.callback()
def _root(
    ctx: typer.Context,
    config: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="配置文件路径；文件不存在时自动创建",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="显示版本号并退出",
    ),
):
    """记录全局配置路径，并在子命令运行前校验模型凭据。"""
    del version
    _CONFIG["path"] = config
    command = ctx.invoked_subcommand
    should_validate = (
        not _CONFIG.get("skip_api_check")
        and command is not None
        and command not in _API_CHECK_EXEMPT_COMMANDS
    )
    if should_validate:
        try:
            _validate_api_configuration()
        except (OSError, RuntimeError, ValueError) as error:
            console.print(f"[red]错误：{error}[/]")
            raise typer.Exit(1) from None


def _load_config() -> Config:
    """加载当前 CLI 调用选定的配置文件。"""
    return Config.load(str(_CONFIG["path"]))


def _validate_api_configuration() -> None:
    """构建当前 provider 并在任何命令逻辑前校验所需 API 凭据。"""
    from .llm.factory import build_client

    build_client(_load_config()).validate_credentials()


def _require_input_file(input_path: str) -> None:
    """确认输入路径是文件，否则打印错误并以状态码 1 退出。"""
    if not os.path.isfile(input_path):
        console.print(f"[red]输入文件不存在：{input_path}[/]")
        raise typer.Exit(1)


def _validate_output_format(fmt: str) -> str:
    """规范化并校验用户可选择的输出格式。"""
    normalized = fmt.strip().lower()
    allowed = {"epub", "txt", "html", "markdown", "pdf"}
    if normalized not in allowed:
        console.print(f"[red]不支持的输出格式：{fmt}（可选 epub / txt / html / markdown / pdf）[/]")
        raise typer.Exit(2)
    return normalized


def _validate_pdf_engine(engine: str) -> str:
    """规范化并校验 PDF 渲染引擎。"""
    normalized = engine.strip().lower()
    if normalized not in {"weasyprint", "fpdf2"}:
        console.print(f"[red]不支持的 PDF 引擎：{engine}（可选 weasyprint / fpdf2）[/]")
        raise typer.Exit(2)
    return normalized


def _runstore_for(config: Config, input_path: str) -> RunStore:
    """解析输入书名并定位其已有状态目录，不主动创建目录。"""
    _require_input_file(input_path)
    if os.path.splitext(input_path)[1].lower() == ".pdf":
        title = os.path.splitext(os.path.basename(input_path))[0]
        run_dir = os.path.join(config.state_dir, slugify(title))
        store = RunStore(run_dir, create=False)
    else:
        doc = load_document(input_path, config.source_lang, config.target_lang)
        run_dir = os.path.join(config.state_dir, slugify(doc.title))
        store = RunStore(run_dir, create=False)
    if store.exists():
        with store.lock():
            store.ensure_source_identity(input_path)
    return store


def _runstore_for_cli(config: Config, input_path: str) -> RunStore:
    """为状态类 CLI 定位状态，并把身份错误转换为简洁提示。"""
    try:
        return _runstore_for(config, input_path)
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None


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
    chapter: int | None = None,
    fmt: str = "epub",
    out: str | None = None,
    pdf_engine: str = "weasyprint",
    polish: bool | None = None,
    review: bool | None = None,
    mono: bool | None = None,
    bilingual: bool | None = None,
) -> None:
    """执行一键翻译流程，并把预期的输入/配置错误转成简洁 CLI 提示。"""
    try:
        _translate_impl_or_raise(
            input_path,
            chapter=chapter,
            fmt=fmt,
            out=out,
            pdf_engine=pdf_engine,
            polish=polish,
            review=review,
            mono=mono,
            bilingual=bilingual,
        )
    except (IngestError, ImportError, OSError, ValueError) as error:
        # CLI 错误保持单行，避免 Rich 按终端宽度拆开可搜索的错误片段。
        console.print(f"[red]错误：{error}[/]", soft_wrap=True)
        raise typer.Exit(1) from None


def _translate_impl_or_raise(
    input_path: str,
    *,
    chapter: int | None = None,
    fmt: str = "epub",
    out: str | None = None,
    pdf_engine: str = "weasyprint",
    polish: bool | None = None,
    review: bool | None = None,
    mono: bool | None = None,
    bilingual: bool | None = None,
) -> None:
    """执行翻译并保留原异常，由 ``_translate_impl`` 转为 CLI 错误。"""
    from .pipeline.orchestrator import Orchestrator

    _require_input_file(input_path)
    fmt = _validate_output_format(fmt)
    pdf_engine = _validate_pdf_engine(pdf_engine)
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
        if mono is not None:
            ignored.append("--mono/--no-mono")
        if bilingual is not None:
            ignored.append("--bilingual/--no-bilingual")
        if ignored:
            raise ValueError(
                "--chapter 只翻译并保存指定章节，不能同时使用收尾选项：" + "、".join(ignored)
            )

    orch = Orchestrator(config)

    # Review 的轮数由模型结果决定；只有单章或无 Review 时才能预测总体进度。
    overall_predictable = chapter is not None or not config.pipeline.review
    with _new_progress() as prog:
        cb = _RichProgressBridge(
            prog,
            "准备中…",
            overall_predictable=overall_predictable,
        )
        try:
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
                pdf_engine=pdf_engine,
            )
        finally:
            _close_progress_activity(getattr(orch, "client", None), cb)

    s = result["report"]["summary"]
    console.print(
        f"[bold green]完成[/]：{s['chapters_done']}/{s['chapters_total']} 章，术语 {s['terms']}。"
    )
    _print_usage({"usage": result["store"].load_usage() or {}})
    for path in result.get("outputs") or [result["output"]]:
        console.print(f"译文：[bold]{path}[/]")
    if result.get("review_dir"):
        review_result = result.get("review_result") or {}
        review_summary = review_result.get("summary") or {}
        console.print(
            f"审校结果：{review_result.get('termination', 'unknown')}，"
            f"问题 {review_summary.get('issue_count', 0)} 项，"
            f"修改建议 {review_summary.get('change_count', 0)} 项。"
        )
        console.print(f"审校目录：{result['review_dir']}")


def _prepare_impl(input_path: str) -> None:
    """完成译前准备并停止，不生成正文译文或输出文件。"""
    from .pipeline.orchestrator import Orchestrator

    try:
        _require_input_file(input_path)
        config = _load_config()
        orch = Orchestrator(config)
        with _new_progress() as prog:
            cb = _RichProgressBridge(prog, "准备中…")
            try:
                store = orch.prepare_for_translation(input_path, progress=cb)
            finally:
                _close_progress_activity(getattr(orch, "client", None), cb)
    except (IngestError, ImportError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None

    manifest = store.load_manifest()
    chapters = manifest.get("chapters", [])
    analysis = store.load_analysis() or {}
    digests = sum(
        bool(store.load_chapter(item["index"]).meta.get("source_digest")) for item in chapters
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
    chapter: int | None = typer.Option(
        None,
        "--chapter",
        min=0,
        help="仅翻译并保存指定章节（从 0 起）；不执行审校、报告和导出",
    ),
    fmt: str = typer.Option(
        "epub",
        "--format",
        help="最终导出格式：epub / txt / html / markdown / pdf",
    ),
    out: str | None = typer.Option(
        None,
        "--out",
        help="单语版输出路径；默认写入源文件旁的 output 目录",
    ),
    pdf_engine: str = typer.Option(
        "weasyprint",
        "--pdf-engine",
        help="PDF 渲染引擎：weasyprint（默认）/ fpdf2",
    ),
    polish: bool | None = typer.Option(
        None,
        "--polish/--no-polish",
        help="覆盖 pipeline.polish，控制翻译后是否润色",
    ),
    review: bool | None = typer.Option(
        None,
        "--review/--no-review",
        help="覆盖 pipeline.review，控制全书翻译后是否执行最终审校",
    ),
    mono: bool | None = typer.Option(
        None,
        "--mono/--no-mono",
        help="覆盖 output.mono，控制是否生成单语版",
    ),
    bilingual: bool | None = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help="覆盖 output.bilingual，控制是否生成原文译文对照版",
    ),
):
    """一键完成准备、翻译、可选审校、报告和导出；中断后原命令续跑。"""
    _translate_impl(
        input,
        chapter=chapter,
        fmt=fmt,
        out=out,
        pdf_engine=pdf_engine,
        polish=polish,
        review=review,
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
):
    """全量运行取证、影子修订与盲复审；不修改正式正文。"""
    from .pipeline.orchestrator import Orchestrator

    _require_input_file(input)
    config = _load_config()
    orch = Orchestrator(config)

    try:
        with _new_progress() as prog:
            cb = _RichProgressBridge(prog, "准备全书审校…")
            try:
                result = orch.run_review(input, progress=cb)
            finally:
                _close_progress_activity(getattr(orch, "client", None), cb)
    except (IngestError, ImportError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None

    review_result = result["review_result"]
    summary = review_result["summary"]
    console.print(
        f"[bold green]全书 Agent 审校完成[/]：{review_result['termination']}，"
        f"仍有 {summary['issue_count']} 项问题，"
        f"生成 {summary['change_count']} 项修改建议。"
    )
    console.print("审校结果为只读建议，正式章节译文未修改。")
    console.print(f"审校目录：{result['review_dir']}")


# ── 查询 / 细粒度命令 ──────────────────────────────────────────────────────
@app.command(rich_help_panel="状态与输出")
def status(
    input: str = typer.Argument(..., help="已建立翻译状态的源文件"),
) -> None:
    """查看各章进度与术语库统计。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for_cli(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 prepare 或 translate。[/]")
        raise typer.Exit(1)
    m = store.load_manifest()
    console.print(f"《{m['title']}》（{m['fmt']}）  {m['source_lang']}→{m['target_lang']}")
    table = Table("", "#", "章节", "翻译")
    for c in m["chapters"]:
        mark = "✓" if c["status"] == STATUS_DONE else "·"
        table.add_row(
            mark,
            str(c["index"]),
            c["title"],
            c["status"],
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
    store = _runstore_for_cli(config, input)
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
    store = _runstore_for_cli(config, input)
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
    store = _runstore_for_cli(config, input)
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
    out: str | None = typer.Option(
        None,
        "--out",
        help="单语版输出路径；默认写入源文件旁的 output 目录",
    ),
    fmt: str = typer.Option(
        "epub",
        "--format",
        help="导出格式：epub / txt / html / markdown / pdf",
    ),
    pdf_engine: str = typer.Option(
        "weasyprint",
        "--pdf-engine",
        help="PDF 渲染引擎：weasyprint（默认）/ fpdf2",
    ),
    mono: bool | None = typer.Option(
        None,
        "--mono/--no-mono",
        help="覆盖 output.mono，控制是否生成单语版",
    ),
    bilingual: bool | None = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help="覆盖 output.bilingual，控制是否生成原文译文对照版",
    ),
):
    """从已有状态重新生成译文文件，不调用模型或重新翻译。"""
    from .llm.providers.fake import FakeClient
    from .pipeline.orchestrator import Orchestrator

    config = _load_config()
    fmt = _validate_output_format(fmt)
    pdf_engine = _validate_pdf_engine(pdf_engine)
    _require_input_file(input)
    if mono is not None:
        config.output.mono = mono
    if bilingual is not None:
        config.output.bilingual = bilingual
    try:
        result = Orchestrator(config, client=FakeClient()).run_assemble(
            input,
            out_format=fmt,
            out_path=out,
            pdf_engine=pdf_engine,
        )
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None
    paths = result["outputs"]
    for path in paths:
        console.print(f"已生成译文：[bold]{path}[/]")


@app.command(rich_help_panel="状态与输出")
def report(
    input: str = typer.Argument(..., help="已建立翻译状态的源文件"),
) -> None:
    """根据当前章节和术语状态重新生成 report.json，不调用模型。"""
    from .llm.providers.fake import FakeClient
    from .pipeline.orchestrator import Orchestrator

    config = _load_config()
    _require_input_file(input)
    try:
        result = Orchestrator(config, client=FakeClient()).run_report(input)
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]错误：{error}[/]")
        raise typer.Exit(1) from None
    store = result["store"]
    rep = result["report"]
    s = rep["summary"]
    console.print(f"翻译报告已写入 {store.report_path}")
    console.print(
        f"  章节 {s['chapters_done']}/{s['chapters_total']}  术语 {s['terms']}  "
        f"待裁决冲突 {s['open_conflicts']}  回译疑点 {s['backtranslation_issues']}"
    )


app.add_typer(glossary_app, name="glossary", rich_help_panel="术语库")


def main() -> None:
    """启动 Typer 命令行应用。"""
    app()


if __name__ == "__main__":
    main()
