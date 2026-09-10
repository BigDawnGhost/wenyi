"""润色 Agent。

逐段对照源文与直译稿做中文文学性二次加工，避免批量响应在段落之间串位。
单段响应在协议重试后仍无效时只保留该段原译文，不丢弃同批其他结果。
"""

from __future__ import annotations

from trans_novel.agents import langprofile, prompts
from trans_novel.agents.base import Agent, WorkflowProtocolError, retry_protocol
from trans_novel.glossary.store import GlossaryTerm


class Polisher(Agent):
    def polish(
        self,
        targets: list[str],
        sources: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        source_contexts: tuple[str, ...] | None = None,
        chapter_title: str = "",
        strict: bool = False,
    ) -> list[str]:
        if len(targets) != len(sources):
            raise ValueError("polish source/target count mismatch")
        if source_contexts is None:
            source_contexts = ("",) * len(targets)
        elif len(source_contexts) != len(targets):
            raise ValueError("polish source context/target count mismatch")
        polished = []
        for source, target, source_context in zip(sources, targets, source_contexts, strict=True):
            if not langprofile.needs_translation(source):
                polished.append(source)
                continue
            system = prompts.render("polisher_system", src=self.src, tgt=self.tgt, n=1)
            user = prompts.render(
                "polisher_user",
                src=self.src,
                tgt=self.tgt,
                glossary=prompts.render_glossary(glossary_terms or []),
                style=style or "（无）",
                source_context=source_context or "（无）",
                chapter_title=chapter_title or "（无）",
                n=1,
                numbered_source=prompts.numbered([source]),
                numbered_target=prompts.numbered([target]),
            )

            def ask_one(system=system, user=user) -> str:
                values = self._ask_json(
                    system,
                    user,
                    key="polished",
                    default=None,
                    agent="editor",
                    operation="polish.segment",
                    strict=strict,
                )
                one = (
                    values[0].strip()
                    if isinstance(values, list)
                    and len(values) == 1
                    and isinstance(values[0], str)
                    and values[0].strip()
                    else ""
                )
                if not one:
                    raise WorkflowProtocolError("polish_item_invalid")
                return one

            try:
                polished.append(
                    retry_protocol(ask_one, retries=self.config.pipeline.protocol_retry_limit)
                    if strict
                    else ask_one()
                )
            except WorkflowProtocolError:
                polished.append(target)
        return polished
