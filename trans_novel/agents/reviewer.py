"""审校 Agent（廉价档）+ 回译抽检。

Reviewer：逐段比对原文/译文，报漏译、增译、误译、术语违例、人称错误。
BackTranslator：把译文回译成源语言，再与原文比对，抽样发现重大语义偏离。
"""

from __future__ import annotations

import json
from typing import Any

from . import langprofile, prompts
from .base import Agent


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
        issues = self.dict_items(
            self._ask_json(system, user, tier="cheap", key="issues"))
        return self._challenge_terminology_issues(
            sources, targets, glossary_terms or [], issues)

    def _challenge_terminology_issues(
        self,
        sources: list[str],
        targets: list[str],
        glossary_terms,
        issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """二次复核术语候选，仅删除被明确判为误报的项目。

        第二次调用只在首轮产生合法下标的 terminology 项时发生。模型调用失败、
        返回缺项/重复项或协议不合法时保守保留首轮结果，避免复核故障造成漏报。
        """
        candidates = []
        for candidate_id, issue in enumerate(issues):
            if str(issue.get("type") or "").strip().lower() != "terminology":
                continue
            index = issue.get("index")
            if isinstance(index, str):
                try:
                    index = int(index.strip())
                except ValueError:
                    continue
            if type(index) is not int or not 0 <= index < len(sources):
                continue
            candidates.append({
                "candidate_id": candidate_id,
                "issue": issue,
            })

        if not candidates:
            return issues

        glossary = prompts.render_glossary(glossary_terms)
        pairs = prompts.numbered_pairs(sources, targets)
        system = prompts.render(
            "terminology_challenger_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "terminology_challenger_user",
            src=self.src,
            tgt=self.tgt,
            glossary=glossary,
            pairs=pairs,
            candidates=json.dumps(candidates, ensure_ascii=False),
        )
        verdicts = self.dict_items(self._ask_json(
            system, user, tier="cheap", key="verdicts", default=[]))

        expected = {item["candidate_id"] for item in candidates}
        resolved: dict[int, str] = {}
        for item in verdicts:
            candidate_id = item.get("candidate_id")
            if isinstance(candidate_id, str):
                try:
                    candidate_id = int(candidate_id.strip())
                except ValueError:
                    return issues
            verdict = str(item.get("verdict") or "").strip().lower()
            rationale = str(item.get("rationale") or "").strip()
            if (
                type(candidate_id) is not int
                or candidate_id not in expected
                or candidate_id in resolved
                or verdict not in {"confirm", "reject", "uncertain"}
                or not rationale
            ):
                return issues
            resolved[candidate_id] = verdict

        if set(resolved) != expected:
            return issues
        rejected = {
            candidate_id
            for candidate_id, verdict in resolved.items()
            if verdict == "reject"
        }
        return [
            issue for candidate_id, issue in enumerate(issues)
            if candidate_id not in rejected
        ]


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
