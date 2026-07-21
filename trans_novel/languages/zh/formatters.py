"""简体中文模型输入中的动态区块格式。"""

from __future__ import annotations

from typing import Any


EMPTY = "（无）"
SAMPLE_LABELS = ("开头样章", "中部样章", "结尾样章")


def render_glossary(terms: list[Any]) -> str:
    """把术语列表渲染成中文提示词中的对照表。"""
    if not terms:
        return "（暂无）"
    lines: list[str] = []
    for term in terms:
        extra = [term.type]
        if term.gender:
            extra.append(term.gender)
        if term.reading:
            extra.append(f"读音: {term.reading}")
        tag = f"（{'，'.join(extra)}）"
        aliases = f" [别名: {', '.join(term.aliases)}]" if term.aliases else ""
        note = f" [说明: {term.note}]" if term.note else ""
        lines.append(f"- {term.source} → {term.target}{tag}{aliases}{note}")
    return "\n".join(lines)


def style_brief(analysis: dict[str, Any], characters: list[dict[str, Any]]) -> str:
    """把分析结果渲染为中文风格与角色简报。"""
    labels = (
        ("genre", "体裁"),
        ("tone", "语气文体"),
        ("style_guide", "风格指南"),
        ("narration", "叙事"),
        ("pacing", "句式节奏"),
        ("register", "语域"),
        ("dialogue_style", "对话风格"),
        ("rhetoric", "修辞"),
    )
    lines = [f"{label}：{analysis[key]}" for key, label in labels if analysis.get(key)]
    if characters:
        lines.append("角色：")
        for character in characters:
            gender = f"，{character.get('gender')}" if character.get("gender") else ""
            note = f"，{character.get('note')}" if character.get("note") else ""
            target = character.get("target", character.get("source", ""))
            lines.append(
                f"  - {target}({character.get('source', '')}{gender}{note})"
            )
    return "\n".join(lines)


def numbered_pairs(sources: list[str], targets: list[str]) -> str:
    """按下标渲染供审校使用的中文原译文对照。"""
    return "\n".join(
        f"[{index}] 原文：{source}\n    译文：{target}"
        for index, (source, target) in enumerate(zip(sources, targets))
    )


def backtranslation_pairs(sources: list[str], backtranslations: list[str]) -> str:
    """按下标渲染供语义核查使用的中文原文/回译对照。"""
    return "\n".join(
        f"[{index}] 原文：{source}\n    回译：{backtranslation}"
        for index, (source, backtranslation) in enumerate(
            zip(sources, backtranslations)
        )
    )


def chapter_label(title: str, index: int) -> str:
    """返回用于模型摘要区块的中文章节标签。"""
    return title.strip() or f"章节 {index + 1}"


def chapter_digest(label: str, snippet: str) -> str:
    """渲染一致性检查中的单章摘要。"""
    return f"[{label}]\n{snippet}"


def join_digest_segments(segments: list[str]) -> str:
    """用中文省略号连接章节首尾片段。"""
    return "……".join(segments)


def autofix_feedback(issues: list[dict[str, Any]]) -> str:
    """把审校问题渲染成定向重译所需的中文反馈。"""
    return "；".join(
        f"{issue.get('detail', '')}（建议：{issue.get('suggestion', '')}）"
        for issue in issues
    )


def sample_block(label: str, text: str) -> str:
    """渲染带中文位置标签的风格分析样章。"""
    return f"【{label}】\n{text}"
