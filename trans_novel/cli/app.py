"""日常只需 `translate` 一个命令：连续全流程（分析→翻译→确定性 QA→报告→回填 EPUB），
中断后再次运行自动续跑。其余 `resume` / `status` 为常用辅助；
细粒度/调试工具收敛到 `tools`：glossary / assemble / qa / report。
"""

from __future__ import annotations

from importlib.metadata import version as package_version

import typer
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from trans_novel.cli import common as cli_common
from trans_novel.cli.tools import tools_app
from trans_novel.config import Config
from trans_novel.pipeline import UsagePersistenceError
from trans_novel.pipeline.execution import ReadinessError, RequiredNodeFailed
from trans_novel.pipeline.state import STATUS_DONE, IdentityMismatchError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="多 Agent 小说翻译系统（多语言 → 中文）",
)
console = cli_common.console
_TRANSLATION_ERRORS = (
    RequiredNodeFailed,
    IdentityMismatchError,
    ReadinessError,
    UsagePersistenceError,
    ValueError,
)


def _show_version(value: bool) -> None:
    if value:
        console.print(f"trans-novel {package_version('trans-novel')}")
        raise typer.Exit()


@app.callback()
def _root(
    config: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_show_version,
        is_eager=True,
        help="显示版本并退出",
    ),
):
    cli_common.set_config_path(config)


def _exit_translation_error(error: Exception) -> None:
    console.print(f"[red]{error}[/]")
    if isinstance(error, RequiredNodeFailed):
        console.print("运行状态已保存；再次运行相同命令即可从失败位置继续。")
    raise typer.Exit(2) from None


def _update_progress(prog, task, done: int, total: int, label: str):
    if not total:
        prog.remove_task(task)
        return prog.add_task(label, total=None, count="")
    prog.update(
        task,
        completed=done,
        total=total,
        description=label,
        count=f"{done}/{total}",
    )
    return task


@app.command("init")
def init_config(
    force: bool = typer.Option(False, "--force", help="覆盖已有配置文件"),
) -> None:
    """生成一份可直接修改的配置文件。"""
    target = cli_common.config_path()
    try:
        created = Config.create_default_file(str(target), overwrite=force)
        if not created:
            console.print(f"[yellow]配置文件已存在：{target}[/]")
            console.print("如需重建，请加 [bold]--force[/]。")
            raise typer.Exit(1)
    except OSError as error:
        console.print(f"[red]无法写入配置文件：{target}[/]")
        console.print(str(error))
        raise typer.Exit(1) from None
    console.print(f"[bold green]已生成配置文件：{target}[/]")
    console.print("下一步：设置 OPENCODE_API_KEY，然后运行 trans-novel translate <小说文件>。")


def _translate_impl(
    input_path: str,
    *,
    chapter: int | None = None,
    fmt: str = "epub",
    out: str | None = None,
    quality: str | None = None,
    source_language: str | None = None,
    honorifics: str | None = None,
    polish: bool | None = None,
    mono: bool | None = None,
    bilingual: bool | None = None,
    prepare: bool = False,
) -> None:
    """translate/resume 共享实现，避免 CLI 参数转发漂移。"""
    from trans_novel.pipeline import Application

    cli_common.require_input_file(input_path)
    fmt = cli_common.validate_output_format(fmt)
    config = cli_common.load_config()
    try:
        if quality is not None:
            config.apply_quality(quality)
        if honorifics is not None:
            if honorifics not in {"keep_style", "normalize", "drop"}:
                raise ValueError("--honorifics 必须是 keep_style、normalize 或 drop")
            config.honorific_strategy = honorifics
    except ValueError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(2) from None
    if source_language is not None:
        config.source_lang = source_language.strip() or "auto"
    if polish is not None:
        config.pipeline.polish = polish
    if mono is not None:
        config.output.mono = mono
    if bilingual is not None:
        config.output.bilingual.enabled = bilingual
    if prepare and chapter is not None:
        console.print("[red]--prepare 不能与 --chapter 同时使用。[/]")
        raise typer.Exit(2)
    application = Application(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.fields[count]}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("准备中…", total=None, count="")

        def cb(done: int, total: int, label: str) -> None:
            nonlocal task
            task = _update_progress(prog, task, done, total, label)

        if prepare:
            try:
                store = application.prepare_for_translation(input_path, progress=cb)
            except _TRANSLATION_ERRORS as error:
                _exit_translation_error(error)
            manifest = store.load_manifest()
            chapters = manifest.get("chapters", [])
            console.print(f"[bold green]准备完成[/]：解析 {len(chapters)} 章。")
            console.print(f"状态目录：[bold]{store.run_dir}[/]")
            console.print("再次运行 translate 命令（不带 --prepare）即可开始翻译。")
            cli_common.print_usage({"usage": store.load_usage() or {}})
            return

        if chapter is not None:
            try:
                store = application.run(input_path, only_chapter=chapter, progress=cb)
            except _TRANSLATION_ERRORS as error:
                _exit_translation_error(error)
            console.print(f"[green]已翻第 {chapter} 章[/]，状态目录：{store.run_dir}")
            cli_common.print_usage({"usage": store.load_usage() or {}})
            return

        try:
            result = application.run_all(
                input_path,
                progress=cb,
                out_format=fmt,
                out_path=out,
            )
        except _TRANSLATION_ERRORS as error:
            _exit_translation_error(error)

    summary = result["report"]["summary"]
    repair = result["report"].get("repair", {})
    if repair.get("deferred"):
        console.print(
            "[yellow]Repair 未完成：模型服务调用失败，译文仍已生成。再次运行 translate 可重试。[/]"
        )
    console.print(
        f"术语 {summary['terms']}，Repair 检测 {repair.get('detected', 0)} 项，"
        f"解决 {repair.get('resolved', 0)} 项，耗尽 {repair.get('accepted_after_exhaustion', 0)} 项。"
    )
    cli_common.print_usage({"usage": result["store"].load_usage() or {}})
    cli_common.print_chapter_processing(result["report"])
    if fmt == "epub":
        cli_common.print_theme_warnings(result["store"].load_epub_verification())
    for path in result.get("outputs") or [result["output"]]:
        console.print(f"译文：[bold]{path}[/]")
    console.print(
        f"[bold green]完成[/]：{summary['chapters_done']}/{summary['chapters_total']} 章，"
        f"Repair 逻辑调用 {repair.get('attempts', 0)} 次。"
    )


@app.command()
def translate(
    input: str = typer.Argument(..., help="输入文件（.epub / .fb2 / .txt / .md）"),
    chapter: int | None = typer.Option(
        None, "--chapter", min=0, help="只翻指定章（从 0 起；调试用，不做收尾）"
    ),
    fmt: str = typer.Option("epub", "--format", help="输出格式：epub | txt"),
    out: str | None = typer.Option(
        None, "--out", help="输出路径（默认 <源文件名>.zh.<ext>，落在源文件目录）"
    ),
    quality: str | None = typer.Option(
        None, "--quality", help="本次运行的质量档位：economy | balanced | quality"
    ),
    source_language: str | None = typer.Option(
        None, "--source-language", help="源语言代码；默认由模型自动识别"
    ),
    honorifics: str | None = typer.Option(
        None, "--honorifics", help="日文敬称策略：keep_style | normalize | drop"
    ),
    polish: bool | None = typer.Option(
        None, "--polish/--no-polish", help="覆盖质量档位中的润色策略"
    ),
    mono: bool | None = typer.Option(None, "--mono/--no-mono", help="是否产出纯中文版"),
    bilingual: bool | None = typer.Option(
        None, "--bilingual/--no-bilingual", help="是否产出双语对照版"
    ),
    prepare: bool = typer.Option(
        False, "--prepare", help="只完成解析、全书预扫和术语定名，不翻译正文"
    ),
):
    """翻译（连续全流程；可断点续跑）。"""
    _translate_impl(
        input,
        chapter=chapter,
        fmt=fmt,
        out=out,
        quality=quality,
        source_language=source_language,
        honorifics=honorifics,
        polish=polish,
        mono=mono,
        bilingual=bilingual,
        prepare=prepare,
    )


@app.command()
def resume(
    input: str = typer.Argument(..., help="输入文件"),
    fmt: str = typer.Option("epub", "--format", help="输出格式：epub | txt"),
):
    """断点续跑（等价于再次 translate）。"""
    _translate_impl(input, fmt=fmt)


@app.command()
def status(input: str = typer.Argument(..., help="输入文件")):
    """查看各章进度与术语库统计。"""
    from trans_novel.glossary.store import GlossaryStore

    config = cli_common.load_config()
    store = cli_common.runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    manifest = store.load_manifest()
    console.print(
        f"《{manifest['title']}》（{manifest['fmt']}）  {manifest['source_lang']}→{manifest['target_lang']}"
    )
    table = Table("", "#", "章节", "状态")
    for chapter in manifest["chapters"]:
        chapter_status = store.chapter_status(chapter["index"])
        mark = "✓" if chapter_status == STATUS_DONE else "·"
        table.add_row(mark, str(chapter["index"]), chapter["title"], chapter_status)
    console.print(table)
    glossary = GlossaryStore(store.glossary_path)
    console.print("术语库：", glossary.stats())
    glossary.close()


app.add_typer(tools_app, name="tools")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
