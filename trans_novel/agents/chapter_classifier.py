"""全源文语义章节分类。"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from trans_novel.agents.base import Agent, WorkflowProtocolError, retry_protocol


class ChapterObservation(BaseModel):
    """一次完整章节或连续源文分块的模型观察。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    chapter_id: int
    kind: Literal["reference_only", "translatable", "uncertain"]
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be empty")
        return value


_SYSTEM_PROMPT = """You classify book chapter SOURCE content for translation handling.
Return exactly one JSON object with chapter_id, kind, and a concise nonempty reason.
kind must be one of reference_only, translatable, uncertain.
reference_only means the source is exclusively citation/reference/bibliography/list material.
Substantive explanation, narrative, or a meaningful appendix is translatable. A mixture of
citation/reference material and substantive explanation is uncertain even when the mixture is
clear, because it requires translation review. Title, context, and structural hints are advisory
context only: never preserve from them alone. Judge every supplied SOURCE character, not keywords
or chapter position."""


class ChapterClassifier(Agent):
    def classify(
        self,
        *,
        chapter_id: int,
        title: str,
        context: str,
        source: str,
        hints: list[str],
    ) -> ChapterObservation:
        user = json.dumps(
            {
                "chapter_id": chapter_id,
                "title_context": title,
                "structural_context": context,
                "semantic_hints": hints,
                "source": source,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def call() -> ChapterObservation:
            data = self._ask_json(
                _SYSTEM_PROMPT,
                user,
                agent="analyst",
                operation="chapter.classify",
                strict=True,
                max_tokens=500,
            )
            try:
                result = ChapterObservation.model_validate(data)
            except ValidationError as error:
                raise WorkflowProtocolError("chapter_classification_invalid") from error
            if result.chapter_id != chapter_id:
                raise WorkflowProtocolError("chapter_classification_id_mismatch")
            return result

        return retry_protocol(call, retries=self.config.pipeline.protocol_retry_limit)


__all__ = ["ChapterClassifier", "ChapterObservation"]
