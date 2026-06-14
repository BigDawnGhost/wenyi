"""命令行入口（typer + rich）。

主命令 translate = 连续全流程：分析 → 翻译（章内串行、逐批刷新上下文、段级进度）→
术语 AI 审计统一 → 一致性 QA → 报告 → 回填出 EPUB，一气呵成；中断后再次运行自动断点续跑。

其余子命令供细粒度使用：resume / status / glossary / assemble / qa / report。
"""

from __future__ import annotations

import os
from typing import Optional

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

from .config import Config
from .ingest.segmenter import load_document
from .pipeline.runstore import RunStore, slugify, STATUS_DONE

app = typer.Typer(add_completion=False, help="多 Agent 小说翻译系统（日/英 → 中）")
console = Console()

_CONFIG = {"path": "config.yaml"}


@app.callback()
def _root(config: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径")):
    _CONFIG["path"] = config


def _load_config() -> Config:
    return Config.load(_CONFIG["path"])


def _runstore_for(config: Config, input_path: str) -> RunStore:
    doc = load_document(input_path, config.source_lang, config.target_lang)
    run_dir = os.path.join(config.state_dir, slugify(doc.title))
    return RunStore(run_dir)


# ── translate / resume：连续全流程 ──────────────────────────────────────────
@app.command()
def translate(
    input: str = typer.Argument(..., help="输入文件（.epub / .txt / .md）"),
    chapter: Optional[int] = typer.Option(None, "--chapter", help="只翻指定章（调试用，不做收尾）"),
    fmt: str = typer.Option("epub", "--format", help="输出格式：epub | txt"),
    out: Optional[str] = typer.Option(None, "--out", help="输出路径（默认 <译名>.<ext>，落在源文件目录）"),
    polish: bool = typer.Option(False, "--polish", help="开启润色（默认关；强档全量二次加工，较烧 token）"),
    no_audit: bool = typer.Option(False, "--no-audit", help="跳过术语 AI 审计统一"),
    no_qa: bool = typer.Option(False, "--no-qa", help="跳过一致性 QA"),
):
    """翻译（连续全流程；可断点续跑）。"""
    from .pipeline.orchestrator import Orchestrator

    config = _load_config()
    config.pipeline.polish = polish
    orch = Orchestrator(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("段"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("准备中…", total=None)

        def cb(done: int, total: int, label: str) -> None:
            prog.update(task, completed=done, total=total or None, description=label)

        if chapter is not None:
            store = orch.run(input, only_chapter=chapter, progress=cb)
            console.print(f"[green]已翻第 {chapter} 章[/]，状态目录：{store.run_dir}")
            return

        result = orch.run_all(
            input, progress=cb, out_format=fmt, out_path=out,
            do_audit=not no_audit, do_qa=not no_qa,
        )

    s = result["report"]["summary"]
    console.print(f"[bold green]完成[/]：{s['chapters_done']}/{s['chapters_total']} 章，"
                  f"术语 {s['terms']}，统一 {len(result['audit'])} 组，"
                  f"一致性问题 {len(result['qa_issues'])} 项。")
    console.print(f"译文：[bold]{result['output']}[/]")


@app.command()
def resume(input: str = typer.Argument(..., help="输入文件"),
           fmt: str = typer.Option("epub", "--format", help="输出格式：epub | txt")):
    """断点续跑（等价于再次 translate）。"""
    translate(input=input, chapter=None, fmt=fmt, out=None,
              polish=False, no_audit=False, no_qa=False)


# ── 查询 / 细粒度命令 ──────────────────────────────────────────────────────
@app.command()
def status(input: str = typer.Argument(..., help="输入文件")):
    """查看各章进度与术语库统计。"""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    m = store.load_manifest()
    console.print(f"《{m['title']}》（{m['fmt']}）  {m['source_lang']}→{m['target_lang']}")
    table = Table("", "#", "章节", "状态")
    for c in m["chapters"]:
        mark = "✓" if c["status"] == STATUS_DONE else "·"
        table.add_row(mark, str(c["index"]), c["title"], c["status"])
    console.print(table)
    g = GlossaryStore(store.glossary_path)
    console.print("术语库：", g.stats())
    g.close()


@app.command()
def glossary(
    input: str = typer.Argument(..., help="输入文件"),
    action: str = typer.Argument("list", help="list | conflicts | audit | lock | resolve"),
    arg1: Optional[str] = typer.Argument(None),
    arg2: Optional[str] = typer.Argument(None),
):
    """术语库管理。audit 自动统一译法并改写正文。"""
    from .glossary.store import GlossaryStore
    from .glossary import resolver

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    try:
        if action == "list":
            table = Table("原文", "译文", "类型", "置信/状态", "锁")
            for t in g.all_terms():
                table.add_row(t.source, t.target,
                              f"{t.type}{'/' + t.gender if t.gender else ''}",
                              f"{t.confidence}{'/' + t.status if t.status != 'ok' else ''}",
                              "🔒" if t.locked else "")
            console.print(table)
        elif action == "conflicts":
            for c in g.open_conflicts():
                console.print(f"  {c['source']}: 现有「{c['existing_target']}」 vs "
                              f"提议「{c['proposed_target']}」（第{c['chapter']}章）")
        elif action == "audit":
            from .agents.glossary_auditor import GlossaryAuditor
            from .llm.base import build_client
            applied = GlossaryAuditor(build_client(config), config).audit(store, g)
            console.print(f"已统一 {len(applied)} 组术语：")
            for u in applied:
                console.print(f"  {u['source']} → [bold]{u['canonical']}[/]"
                              f"（替换 {', '.join(u['variants']) or '—'}）")
        elif action == "lock":
            resolver.lock(g, arg1)
            console.print(f"已锁定 {arg1} → {g.get_term(arg1).target}")
        elif action == "resolve":
            resolver.resolve(g, arg1, arg2)
            console.print(f"已裁定并锁定 {arg1} → {arg2}")
        else:
            console.print(f"[red]未知 glossary 子命令：{action}[/]")
            raise typer.Exit(1)
    finally:
        g.close()


@app.command()
def assemble(input: str = typer.Argument(..., help="输入文件"),
             out: Optional[str] = typer.Option(None, "--out"),
             fmt: str = typer.Option("epub", "--format", help="epub | txt")):
    """回填生成译文文件（默认 EPUB）。"""
    from .assemble.writer import assemble as do_assemble

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    path = do_assemble(store, input, out_path=out, out_format=fmt)
    console.print(f"已生成译文：[bold]{path}[/]")


@app.command()
def qa(input: str = typer.Argument(..., help="输入文件")):
    """全书跨章一致性扫描。"""
    from .agents.consistency import ConsistencyChecker
    from .glossary.store import GlossaryStore
    from .llm.base import build_client

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    issues = ConsistencyChecker(build_client(config), config).check(store, g)
    g.close()
    console.print(f"一致性问题 {len(issues)} 项：")
    for it in issues:
        console.print(f"  [{it.get('type')}] {it.get('detail')}  ({it.get('where', '')})")


@app.command()
def report(input: str = typer.Argument(..., help="输入文件")):
    """生成 QA 报告（漏译/冲突/低置信度汇总）。"""
    from .assemble.report import build_report
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for(config, input)
    if not store.exists():
        console.print("[yellow]尚无进度。先运行 translate。[/]")
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    rep = build_report(store, g)
    g.close()
    store.save_report(rep)
    s = rep["summary"]
    console.print(f"QA 报告已写入 {store.report_path}")
    console.print(f"  章节 {s['chapters_done']}/{s['chapters_total']}  术语 {s['terms']}  "
                  f"待裁决冲突 {s['open_conflicts']}  审校问题 {s['review_issues']}  "
                  f"回译疑点 {s['backtranslation_issues']}")


@app.command()
def web(
    input: Optional[str] = typer.Argument(None, help="可选：预填的输入文件路径"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
):
    """启动可视化 Web 前端（翻译过程实时双语对照、术语表编辑、建议/修订）。"""
    import uvicorn

    from .web.server import create_app

    application = create_app(config_path=_CONFIG["path"], default_input=input)
    console.print(f"[bold green]Web 前端已启动[/]：http://{host}:{port}")
    if input:
        console.print(f"已预填输入：{input}")
    uvicorn.run(application, host=host, port=port, log_level="info")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
