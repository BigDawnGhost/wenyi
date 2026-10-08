"""Three independent drafts and one source-aware synthesis, without extra model rounds."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from ..glossary.store import GlossaryTerm
from ..llm.base import Messages
from ..llm.json_parser import JsonParseError, parse_json_loose
from ..llm.retrying import TruncatedResponseError
from . import prompts
from .base import Agent
from .translator import Translator

PrecisionCallRecorder = Callable[
    [str, Messages, Callable[[], str], Callable[[str], list[str]]],
    tuple[list[str], str],
]


class PrecisionError(ValueError):
    """A precision response cannot safely be used for document backfill."""


@dataclass(frozen=True)
class PrecisionInputs:
    sources: tuple[str, ...]
    terms: tuple[GlossaryTerm, ...]
    context: str
    style: str
    book_synopsis: str
    chapter_digest: str
    annotation_contexts: list[list[dict[str, str]]]
    next_source: str
    allow_empty_translations: bool = False


class PrecisionAgent(Agent):
    """Keep a stable source prefix; synthesis receives drafts as data, not prior chat turns."""

    def __init__(self, client, config, *, recorder: PrecisionCallRecorder | None = None):
        super().__init__(client, config)
        self._recorder = recorder
        self.last_call_ref: str | None = None

    def _packet(self, inputs: PrecisionInputs) -> str:
        annotations = Translator._validate_annotation_contexts(
            list(inputs.sources), inputs.annotation_contexts
        )
        return self.render(
            "precision_packet",
            packet=json.dumps(
                {
                    "style": inputs.style,
                    "book_synopsis": inputs.book_synopsis,
                    "glossary": prompts.render_glossary(list(inputs.terms), source_lang=self.src),
                    "chapter_digest": inputs.chapter_digest,
                    "context": inputs.context,
                    "sources": inputs.sources,
                    "annotations": json.loads(prompts.render_annotation_contexts(annotations)),
                    "next_source": inputs.next_source,
                    "allow_empty_translations": inputs.allow_empty_translations,
                },
                ensure_ascii=False,
            ),
        )

    def _call(self, inputs: PrecisionInputs, task: str, payload: dict, operation: str) -> list[str]:
        if not any(Translator._needs_translation(source) for source in inputs.sources):
            return list(inputs.sources)
        messages = [
            {"role": "system", "content": self.render("precision_generation_system")},
            {"role": "user", "content": self._packet(inputs)},
            {
                "role": "user",
                "content": self.render(
                    f"precision_{task}", task=json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]

        def invoke() -> str:
            # Match standard translation: leave output limits to the model/provider configuration.
            return self.client.complete(messages, operation=operation, json_mode=True)

        def validate(raw: str) -> list[str]:
            data = parse_json_loose(raw)
            return self._targets(
                inputs, data.get("translations") if isinstance(data, dict) else None
            )

        try:
            if self._recorder:
                targets, self.last_call_ref = self._recorder(operation, messages, invoke, validate)
                return targets
            return validate(invoke())
        except (JsonParseError, TruncatedResponseError) as error:
            raise PrecisionError(f"{operation}: invalid or truncated precision response") from error

    @staticmethod
    def _targets(inputs: PrecisionInputs, targets: object) -> list[str]:
        """Check structure once for backfill; never retry or split model requests."""
        if not isinstance(targets, list) or len(targets) != len(inputs.sources):
            raise PrecisionError("Precision output count does not match the document segments")
        for index, (source, target) in enumerate(zip(inputs.sources, targets)):
            if not isinstance(target, str):
                raise PrecisionError(f"Precision output {index} must be a string")
            if not inputs.allow_empty_translations and source.strip() and not target.strip():
                raise PrecisionError(f"Precision output {index} must not be blank")
            if not Translator._needs_translation(source) and target != source:
                raise PrecisionError(f"Precision output changed protected segment {index}")
        return targets

    def translate(self, inputs: PrecisionInputs) -> list[str]:
        return self._call(
            inputs,
            "translate",
            {"segments": list(range(len(inputs.sources)))},
            "translation.body",
        )

    def synthesize(self, inputs: PrecisionInputs, drafts: list[list[str]]) -> list[str]:
        if len(drafts) != 3:
            raise PrecisionError("Precision synthesis requires three initial drafts")
        for draft in drafts:
            self._targets(inputs, draft)
        # Keep all draws in checkpoints; identical text is not additional evidence.
        distinct = [list(draft) for draft in dict.fromkeys(tuple(draft) for draft in drafts)]
        return self._call(
            inputs,
            "synthesize",
            {"segments": list(range(len(inputs.sources))), "drafts": distinct},
            "polish.body",
        )
