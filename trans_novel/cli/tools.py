"""Advanced and diagnostic command-line tools."""

from __future__ import annotations

import typer

from trans_novel.benchmark.cli import benchmark_app
from trans_novel.cli import common as cli_common
from trans_novel.pipeline.execution import ReadinessError, RequiredNodeFailed
from trans_novel.pipeline.quality import lock, open_glossary, resolve
from trans_novel.pipeline.state import IdentityMismatchError

tools_app = typer.Typer(
    add_completion=False,
    help="高级/调试工具：glossary（术语表）/ assemble（回填）/ qa / report",
)
console = cli_common.console


@tools_app.command()
def glossary(
    input: str = typer.Argument(..., help="输入文件"),
    action: str = typer.Argument("list", help="list | conflicts | audit | lock | resolve"),
    arg1: str | None = typer.Argument(None),
    arg2: str | None = typer.Argument(None),
):
    """术语库管理。audit 自动统一译法并改写正文。"""
    config = cli_common.load_config()
    store = cli_common.runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    with open_glossary(store.glossary_path) as g:
        if action == "list":
            from rich.table import Table

            table = Table("原文", "译文", "类型", "置信/状态", "锁")
            for t in g.all_terms():
                table.add_row(
                    t.source,
                    t.target,
                    f"{t.type}{'/' + t.gender if t.gender else ''}",
                    f"{t.confidence}{'/' + t.status if t.status != 'ok' else ''}",
                    "🔒" if t.locked else "",
                )
            console.print(table)
        elif action == "conflicts":
            for c in g.open_conflicts():
                console.print(
                    f"  {c['source']}: 现有「{c['existing_target']}」 vs "
                    f"提议「{c['proposed_target']}」（第{c['chapter']}章）"
                )
        elif action == "audit":
            from trans_novel.pipeline import Application

            applied = Application(config).glossary_audit(store)
            console.print(f"已统一 {len(applied)} 组术语：")
            for u in applied:
                console.print(
                    f"  {u['source']} → [bold]{u['canonical']}[/]"
                    f"（替换 {', '.join(u['variants']) or '—'}）"
                )
        elif action == "lock":
            if arg1 is None:
                console.print("[red]lock 需要提供原文术语。[/]")
                raise typer.Exit(1)
            lock(g, arg1)
            term = g.get_term(arg1)
            if term is None:
                console.print(f"[red]术语不存在：{arg1}[/]")
                raise typer.Exit(1)
            console.print(f"已锁定 {arg1} → {term.target}")
        elif action == "resolve":
            if arg1 is None or arg2 is None:
                console.print("[red]resolve 需要提供原文术语和目标译名。[/]")
                raise typer.Exit(1)
            resolve(g, arg1, arg2)
            console.print(f"已裁定并锁定 {arg1} → {arg2}")
        else:
            console.print(f"[red]未知 glossary 子命令：{action}[/]")
            raise typer.Exit(1)


@tools_app.command()
def assemble(
    input: str = typer.Argument(..., help="输入文件"),
    out: str | None = typer.Option(None, "--out"),
    fmt: str = typer.Option("epub", "--format", help="epub | txt"),
    mono: bool | None = typer.Option(
        None,
        "--mono/--no-mono",
        help="覆盖配置文件中的单语版产出开关",
    ),
    bilingual: bool | None = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help="覆盖配置文件中的双语版产出开关",
    ),
    reanalyze_layout: bool = typer.Option(
        False,
        "--reanalyze-layout",
        help="忽略已接受的 EPUB 布局分析并重新分析",
    ),
):
    """回填生成译文文件（默认 EPUB）。"""
    from trans_novel.pipeline import Application

    config = cli_common.load_config()
    fmt = cli_common.validate_output_format(fmt)
    store = cli_common.runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    app = Application(config)
    try:
        paths = app.assemble(
            store,
            input,
            out_format=fmt,
            out_path=out,
            mono=mono,
            bilingual=bilingual,
            reanalyze_layout=reanalyze_layout,
            progress=lambda _done, _total, message: console.print(message),
        )
    except (IdentityMismatchError, ReadinessError, RequiredNodeFailed) as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(2) from error
    if fmt == "epub":
        cli_common.print_theme_warnings(store.load_epub_verification())
    for path in paths:
        console.print(f"已生成译文：[bold]{path}[/]")


@tools_app.command()
def qa(input: str = typer.Argument(..., help="输入文件")):
    """全书确定性检查。"""
    from trans_novel.pipeline import Application

    config = cli_common.load_config()
    store = cli_common.runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    issues = Application(config).qa(store)
    console.print(f"一致性问题 {len(issues)} 项：")
    for it in issues:
        console.print(f"  [{it.get('type')}] {it.get('detail')}  ({it.get('where', '')})")


@tools_app.command()
def report(input: str = typer.Argument(..., help="输入文件")):
    """生成 QA 报告（漏译/冲突/低置信度汇总）。"""
    from trans_novel.pipeline import Application

    config = cli_common.load_config()
    store = cli_common.runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    rep = Application(config).report(store)
    summary = rep["summary"]
    repair = rep.get("repair", {})
    console.print(f"QA 报告已写入 {store.report_path}")
    console.print(
        f"  章节 {summary['chapters_done']}/{summary['chapters_total']} 术语 {summary['terms']} "
        f"Repair 检测 {repair.get('detected', 0)} 解决 {repair.get('resolved', 0)} "
        f"耗尽 {repair.get('accepted_after_exhaustion', 0)} 调用 {repair.get('attempts', 0)}"
    )
    cli_common.print_chapter_processing(rep)


tools_app.add_typer(benchmark_app, name="benchmark")


__all__ = ["assemble", "glossary", "qa", "report", "tools_app"]
