"""正式回填的单一权威就绪门禁：运行身份核验 + 完整度检查。

run_all 与 tools assemble 的每条正式产出路径都必须先过这里。不完整状态直接
拒绝，绝不静默回退成“部分原文 + 部分译文”的混合产物。
"""

from __future__ import annotations

from trans_novel.pipeline.contracts import ReadinessError
from trans_novel.pipeline.state import (
    BEST_EFFORT_NODES,
    NODE_ANALYZE,
    NODE_ASSEMBLE,
    NODE_DETERMINISTIC_QA,
    NODE_FAILED_PERMANENT,
    NODE_FAILED_RETRYABLE,
    NODE_LAYOUT,
    NODE_POLISH,
    NODE_PREPARE,
    NODE_REPAIR,
    NODE_REPORT,
    NODE_SKIPPED,
    NODE_SUCCEEDED,
    NODE_TITLES,
    NODE_TRANSLATE,
    STATUS_DONE,
    RunStore,
    chapter_node_key,
    source_bytes_hash,
)


def _chapter_node_base(key: str) -> str:
    return key.split(":", 1)[0]


def assemble_readiness_problems(store: RunStore, *, require_output_nodes: bool = True) -> list[str]:
    """返回阻止正式回填的全部问题（空列表 = 可以回填）。

    覆盖未完成章节、空 target、待润色批次，以及适用的必需上游节点。
    尽力而为的术语节点失败/缺失不阻塞产出；禁用润色被视为已决策。
    ``require_output_nodes=False`` 仅允许重新生成排版、报告与回填结果。
    """
    state = store.load_state()
    problems: list[str] = []

    for idx in state.chapters:
        pg = state.progress.get(idx.index)
        if pg is None or pg.status != STATUS_DONE:
            problems.append(f"第{idx.index}章未完成翻译")
            continue
        if pg.pending_polish:
            problems.append(f"第{idx.index}章润色待完成")

    # 必需 target 非空：done 章的正文段都必须有译文（旁路章 target=source 同样非空）。
    for idx in state.chapters:
        pg = state.progress.get(idx.index)
        if pg is None or pg.status != STATUS_DONE:
            continue
        chapter = store.load_chapter(idx.index)
        for seg in chapter.text_segments:
            if not (seg.target and seg.target.strip()):
                problems.append(f"第{idx.index}章存在未翻译段落")
                break

    # 核心必需链（prepare/analyze/translate）只接受 succeeded；
    # titles/report 仅在正式链路已参与（状态存在）时要求 succeeded。
    def check_book(node_id: str, *, allow_skipped: bool = False) -> None:
        node = state.nodes.get(node_id)
        if node is None:
            problems.append(f"节点 {node_id} 未执行（必需上游未完成）")
        elif node.status == NODE_SUCCEEDED or (allow_skipped and node.status == NODE_SKIPPED):
            return
        else:
            problems.append(f"节点 {node_id} 状态为 {node.status}（必需上游未完成）")

    required_book_nodes = [
        NODE_PREPARE,
        NODE_ANALYZE,
        NODE_TITLES,
        NODE_DETERMINISTIC_QA,
        NODE_REPAIR,
    ]
    if require_output_nodes:
        required_book_nodes.append(NODE_REPORT)
    for node_id in required_book_nodes:
        check_book(node_id)

    for idx in state.chapters:
        pg = state.progress.get(idx.index)
        if pg is None or pg.status != STATUS_DONE:
            continue
        key = chapter_node_key(NODE_TRANSLATE, idx.index)
        node = state.nodes.get(key)
        if node is None or node.status != NODE_SUCCEEDED:
            problems.append(f"节点 {key} 未完成")
        preserved = idx.processing is not None and idx.processing.action == "preserve"
        legacy_bypass = idx.processing is None and pg.back_matter_mode is not None
        if not preserved and not legacy_bypass:
            polish = state.nodes.get(chapter_node_key(NODE_POLISH, idx.index))
            if polish is None or polish.status not in (NODE_SUCCEEDED, NODE_SKIPPED):
                problems.append(f"节点 {chapter_node_key(NODE_POLISH, idx.index)} 未完成")

    retryable_output_nodes = {NODE_LAYOUT, NODE_REPORT, NODE_ASSEMBLE}
    for key, node in sorted(state.nodes.items()):
        if node.status in (NODE_FAILED_RETRYABLE, NODE_FAILED_PERMANENT):
            if not require_output_nodes and _chapter_node_base(key) in retryable_output_nodes:
                continue
            if _chapter_node_base(key) in BEST_EFFORT_NODES:
                continue
            problems.append(f"节点 {node.node_id} 处于失败状态")
    return problems


def ensure_assemble_ready(store: RunStore, source_path: str) -> None:
    """正式回填前的统一门禁：先核验运行身份，再检查完整度。

    源文件失配抛 IdentityMismatchError（翻译/回填复用前都必须拒绝）；其余
    完整度问题抛 ReadinessError。
    """
    state = store.load_state()
    # 回填以状态里的语言为权威（产物是状态的一部分），此处只核验源文件字节。
    store.verify_identity(
        source_bytes_sha256=source_bytes_hash(source_path),
        source_lang=state.identity.source_lang,
        target_lang=state.identity.target_lang,
    )
    problems = assemble_readiness_problems(store)
    if problems:
        raise ReadinessError("；".join(problems))
