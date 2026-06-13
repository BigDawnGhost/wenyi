"""命令行入口。

  trans-novel translate <输入> [--config c.yaml] [--chapter N]
  trans-novel resume    <输入>            # 断点续跑（跳过已完成章节）
  trans-novel status    <输入>            # 查看各章进度
  trans-novel glossary  <输入> list|conflicts|review|lock <源词>|resolve <源词> <译法>
  trans-novel assemble  <输入> [--out 路径]   # 回填生成译文 EPUB/TXT
  trans-novel qa        <输入>            # 全书跨章一致性扫描
  trans-novel report    <输入>            # 生成 QA 报告
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import Config
from .ingest.segmenter import load_document
from .pipeline.runstore import RunStore, slugify, STATUS_DONE


def _runstore_for(config: Config, input_path: str) -> RunStore:
    doc = load_document(input_path, config.source_lang, config.target_lang)
    run_dir = os.path.join(config.state_dir, slugify(doc.title))
    return RunStore(run_dir)


def _cmd_translate(args, config: Config) -> int:
    from .pipeline.orchestrator import Orchestrator
    orch = Orchestrator(config)
    only = args.chapter if getattr(args, "chapter", None) is not None else None
    store = orch.run(args.input, only_chapter=only)
    m = store.load_manifest()
    done = sum(1 for c in m["chapters"] if c["status"] == STATUS_DONE)
    print(f"完成：{done}/{len(m['chapters'])} 章。状态目录：{store.run_dir}")
    return 0


def _cmd_status(args, config: Config) -> int:
    store = _runstore_for(config, args.input)
    if not store.exists():
        print("尚无进度。先运行 translate。")
        return 1
    m = store.load_manifest()
    print(f"《{m['title']}》（{m['fmt']}）  {m['source_lang']}→{m['target_lang']}")
    for c in m["chapters"]:
        mark = "✓" if c["status"] == STATUS_DONE else "·"
        print(f"  {mark} [{c['index']}] {c['title']}  {c['status']}")
    from .glossary.store import GlossaryStore
    g = GlossaryStore(store.glossary_path)
    print("术语库：", g.stats())
    g.close()
    return 0


def _cmd_glossary(args, config: Config) -> int:
    from .glossary.store import GlossaryStore
    from .glossary import resolver
    store = _runstore_for(config, args.input)
    if not store.exists():
        print("尚无进度。先运行 translate。")
        return 1
    g = GlossaryStore(store.glossary_path)
    try:
        action = args.action
        if action == "list":
            for t in g.all_terms():
                lock = "🔒" if t.locked else ""
                print(f"  {t.source} → {t.target}  ({t.type}{'/' + t.gender if t.gender else ''}) "
                      f"[{t.confidence}{'/' + t.status if t.status != 'ok' else ''}]{lock}")
        elif action == "conflicts":
            for c in g.open_conflicts():
                print(f"  {c['source']}: 现有「{c['existing_target']}」 vs 提议「{c['proposed_target']}」"
                      f"（第{c['chapter']}章）")
        elif action == "review":
            data = resolver.pending_review(g)
            print(f"待裁决冲突 {len(data['conflicts'])} 项，低置信度术语 {len(data['low_confidence'])} 项：")
            for c in data["conflicts"]:
                print(f"  冲突 {c['source']}: {c['existing_target']} / {c['proposed_target']}")
            for t in data["low_confidence"]:
                print(f"  低置信 {t['source']} → {t['target']} [{t['confidence']}/{t['status']}]")
        elif action == "lock":
            resolver.lock(g, args.arg1)
            print(f"已锁定 {args.arg1} → {g.get_term(args.arg1).target}")
        elif action == "resolve":
            resolver.resolve(g, args.arg1, args.arg2)
            print(f"已裁定并锁定 {args.arg1} → {args.arg2}")
        else:
            print("未知 glossary 子命令")
            return 1
    finally:
        g.close()
    return 0


def _cmd_assemble(args, config: Config) -> int:
    from .assemble.writer import assemble
    store = _runstore_for(config, args.input)
    if not store.exists():
        print("尚无进度。先运行 translate。")
        return 1
    out = assemble(store, args.input, out_path=args.out, out_format=args.format)
    print(f"已生成译文：{out}")
    return 0


def _cmd_qa(args, config: Config) -> int:
    from .agents.consistency import ConsistencyChecker
    from .glossary.store import GlossaryStore
    from .llm.base import build_client
    store = _runstore_for(config, args.input)
    if not store.exists():
        print("尚无进度。先运行 translate。")
        return 1
    g = GlossaryStore(store.glossary_path)
    checker = ConsistencyChecker(build_client(config), config)
    issues = checker.check(store, g)
    g.close()
    print(f"一致性问题 {len(issues)} 项：")
    for it in issues:
        print(f"  [{it.get('type')}] {it.get('detail')}  ({it.get('where','')})")
    return 0


def _cmd_report(args, config: Config) -> int:
    from .assemble.report import build_report
    from .glossary.store import GlossaryStore
    store = _runstore_for(config, args.input)
    if not store.exists():
        print("尚无进度。先运行 translate。")
        return 1
    g = GlossaryStore(store.glossary_path)
    report = build_report(store, g)
    g.close()
    store.save_report(report)
    s = report["summary"]
    print(f"QA 报告已写入 {store.report_path}")
    print(f"  章节 {s['chapters_done']}/{s['chapters_total']}  术语 {s['terms']}  "
          f"待裁决冲突 {s['open_conflicts']}  审校问题 {s['review_issues']}  回译疑点 {s['backtranslation_issues']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trans-novel", description="多 Agent 日译中小说翻译")
    p.add_argument("--config", default="config.yaml", help="配置文件路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("translate", help="翻译（新建或续跑）")
    pt.add_argument("input")
    pt.add_argument("--chapter", type=int, default=None, help="只翻指定章（调试用）")
    pt.set_defaults(func=_cmd_translate)

    pr = sub.add_parser("resume", help="断点续跑")
    pr.add_argument("input")
    pr.set_defaults(func=_cmd_translate, chapter=None)

    ps = sub.add_parser("status", help="查看进度")
    ps.add_argument("input")
    ps.set_defaults(func=_cmd_status)

    pg = sub.add_parser("glossary", help="术语库管理")
    pg.add_argument("input")
    pg.add_argument("action", choices=["list", "conflicts", "review", "lock", "resolve"])
    pg.add_argument("arg1", nargs="?", default=None)
    pg.add_argument("arg2", nargs="?", default=None)
    pg.set_defaults(func=_cmd_glossary)

    pa = sub.add_parser("assemble", help="回填生成译文文件（默认 EPUB）")
    pa.add_argument("input")
    pa.add_argument("--out", default=None)
    pa.add_argument("--format", choices=["epub", "txt"], default="epub",
                    help="输出格式，默认 epub")
    pa.set_defaults(func=_cmd_assemble)

    pq = sub.add_parser("qa", help="全书一致性扫描")
    pq.add_argument("input")
    pq.set_defaults(func=_cmd_qa)

    prep = sub.add_parser("report", help="生成 QA 报告")
    prep.add_argument("input")
    prep.set_defaults(func=_cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
