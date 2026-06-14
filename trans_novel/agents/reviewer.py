"""审校 Agent（廉价档）+ 回译抽检。

Reviewer：逐段比对原文/译文，报漏译、增译、误译、术语违例、人称错误。
BackTranslator：把译文回译成日文，再与原文比对，抽样发现重大语义偏离。
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..llm.base import LLMClient
from . import langprofile, prompts


def _backtrans_compare_system(src: str) -> str:
    lbl = langprofile.label(src)
    return (
        f"你是翻译保真度核查员。给定原文（{lbl}）与由译文回译得到的{lbl}，"
        "判断两者语义是否一致。只报实质性偏离（信息缺失、含义改变），忽略措辞差异。"
        '仅输出 JSON：{"issues":[{"index":整数,"detail":"偏离描述"}]}，无偏离则 {"issues":[]}。'
    )


class Reviewer:
    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang

    def review(self, sources: list[str], targets: list[str],
               glossary_terms=None) -> list[dict[str, Any]]:
        """返回问题列表：[{index,type,detail,suggestion}]。"""
        if not sources:
            return []
        glossary_terms = glossary_terms or []
        system = prompts.render("reviewer_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "reviewer_user", src=self.src, tgt=self.tgt,
            glossary=prompts.render_glossary(glossary_terms),
            n=len(sources),
            pairs=prompts.numbered_pairs(sources, targets),
        )
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tier="cheap",
            )
        except Exception:
            return []
        issues = data.get("issues", []) if isinstance(data, dict) else (data or [])
        return [i for i in issues if isinstance(i, dict)]


class BackTranslator:
    """回译抽检（廉价档）。两步：译文→日文，再与原文比对。"""

    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang

    def backtranslate(self, targets: list[str]) -> list[str]:
        if not targets:
            return []
        system = prompts.render("backtranslate_system", src=self.src, tgt=self.tgt)
        user = prompts.render("backtranslate_user", src=self.src, tgt=self.tgt,
                              n=len(targets), numbered_target=prompts.numbered(targets))
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tier="cheap",
            )
        except Exception:
            return []
        items = data.get("backtranslations", []) if isinstance(data, dict) else (data or [])
        return [str(x) for x in items] if isinstance(items, list) else []

    def check(self, sources: list[str], targets: list[str]) -> list[dict[str, Any]]:
        """对给定（已抽样的）段做回译并比对，返回偏离问题。index 为传入列表内的下标。"""
        back = self.backtranslate(targets)
        if len(back) != len(sources):
            return []  # 回译对齐失败则跳过，不阻塞
        pairs = "\n".join(
            f"[{i}] 原文：{s}\n    回译：{b}" for i, (s, b) in enumerate(zip(sources, back))
        )
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": _backtrans_compare_system(self.src)},
                 {"role": "user", "content": pairs}],
                tier="cheap",
            )
        except Exception:
            return []
        issues = data.get("issues", []) if isinstance(data, dict) else (data or [])
        return [i for i in issues if isinstance(i, dict)]
