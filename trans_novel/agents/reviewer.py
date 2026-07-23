"""审校 Agent（廉价档）+ 回译抽检。

Reviewer：逐段比对原文/译文，报漏译、增译、误译、术语违例、人称错误。
BackTranslator：把译文回译成源语言，再与原文比对，抽样发现重大语义偏离。
"""

from __future__ import annotations

from typing import Any

from . import langprofile, prompts
from .base import Agent


class ReviewOutputError(ValueError):
    """审校模型返回了可通过缩小输入重试的结构化输出错误。"""

    def __init__(self, reason: str):
        super().__init__(f"审校输出协议错误：{reason}")
        self.reason = reason


def _backtrans_compare_system(src: str) -> str:
    """生成回译语义比对所需的系统提示词。"""
    lbl = langprofile.label(src)
    return (
        f"你是翻译保真度核查员。给定原文（{lbl}）与由译文回译得到的{lbl}，"
        "判断两者语义是否一致。只报实质性偏离（信息缺失、含义改变），忽略措辞差异。"
        '仅输出 JSON：{"issues":[{"index":整数,"detail":"偏离描述"}]}，无偏离则 {"issues":[]}。'
    )


class Reviewer(Agent):
    def review(self, sources: list[str], targets: list[str],
               glossary_terms=None) -> list[dict[str, Any]]:
        """返回问题列表：[{index,type,detail,suggestion}]。"""
        if not sources:
            return []
        system = prompts.render("reviewer_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "reviewer_user", src=self.src, tgt=self.tgt,
            glossary=prompts.render_glossary(glossary_terms or []),
            n=len(sources),
            pairs=prompts.numbered_pairs(sources, targets),
        )
        try:
            data = self._ask_json(system, user, tier="cheap")
        except ValueError as error:
            # parse_json_loose 的异常包含模型原始输出片段；在这里转换成不携带
            # 原译文内容的稳定原因，供编排器拆分恢复和安全记录。
            raise ReviewOutputError("malformed_json") from error
        if isinstance(data, dict):
            issues = data.get("issues")
        elif isinstance(data, list):
            # 兼容旧行为：模型省略 {"issues": ...} 外壳但返回了完整数组时
            # 可直接使用，不必为了纯包装差异增加恢复调用。
            issues = data
        else:
            raise ReviewOutputError("response_not_object")
        if not isinstance(issues, list):
            raise ReviewOutputError("issues_not_list")
        if any(not isinstance(item, dict) for item in issues):
            raise ReviewOutputError("issue_not_object")
        return list(issues)


class BackTranslator(Agent):
    """回译抽检（廉价档）。两步：译文→源语言，再与原文比对。"""

    def backtranslate(self, targets: list[str]) -> list[str]:
        """把目标语言段落批量回译成源语言；失败时返回空列表。"""
        if not targets:
            return []
        system = prompts.render("backtranslate_system", src=self.src, tgt=self.tgt)
        user = prompts.render("backtranslate_user", src=self.src, tgt=self.tgt,
                              n=len(targets), numbered_target=prompts.numbered(targets))
        items = self._ask_json(system, user, tier="fast",  # 机械回译免思考；语义比对(check)仍走 cheap
                               key="backtranslations", default=[])
        return [str(x) for x in items] if isinstance(items, list) else []

    def check(self, sources: list[str], targets: list[str]) -> list[dict[str, Any]]:
        """对给定（已抽样的）段做回译并比对，返回偏离问题。index 为传入列表内的下标。"""
        back = self.backtranslate(targets)
        if len(back) != len(sources):
            return []  # 回译对齐失败则跳过，不阻塞
        pairs = "\n".join(
            f"[{i}] 原文：{s}\n    回译：{b}" for i, (s, b) in enumerate(zip(sources, back))
        )
        return self.dict_items(
            self._ask_json(_backtrans_compare_system(self.src), pairs,
                           tier="cheap", key="issues", default=[]))
