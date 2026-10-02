"""Immutable policy contracts without workflow, model-client or storage dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Point = Literal[
    "prompt.compose",
    "candidate.validate",
    "export.text",
    "export.source_markup",
    "export.style",
    "export.metadata",
]
Role = Literal["source", "target", "pair"]
Phase = Literal["analysis", "translation", "review", "export"]


def content_hash(value: Any) -> str:
    """Hash effective JSON content, independently of dictionary insertion order."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class OperationBinding(BaseModel):
    """Strict developer/profile binding; options are validated against the registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["auto", "on", "off"] = "auto"
    options: dict[str, Any] = Field(default_factory=dict)


class ExportDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    language_tag: str | None = Field(default=None, min_length=1)
    about_locale: Literal["en", "zh"] | None = None

    @field_validator("language_tag", "about_locale", mode="before")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Export policy defaults cannot be null")
        return value


class ProfilePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: dict[str, OperationBinding] = Field(default_factory=dict)
    target: dict[str, OperationBinding] = Field(default_factory=dict)
    export: ExportDefaults = Field(default_factory=ExportDefaults)


class PairPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operations: dict[str, OperationBinding] = Field(default_factory=dict)
    export: ExportDefaults = Field(default_factory=ExportDefaults)


@dataclass(frozen=True)
class OperationSpec:
    id: str
    point: Point
    implementation_version: str = "1"
    contract_version: int = 1
    roles: tuple[Role, ...] = ("source", "target", "pair")
    languages: tuple[str, ...] = ()
    paths: tuple[str, ...] = ("book", "srt")
    formats: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    options_schema: tuple[tuple[str, str, str], ...] = ()
    requires: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()
    exclusive_group: str = ""
    writes: tuple[str, ...] = ()
    failure_policy: Literal["stop", "report"] = "stop"
    llm_operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyContext:
    source: str
    target: str
    phase: Phase = "translation"
    path: Literal["book", "srt"] = "book"
    format: str = ""
    backend: str = "native"
    punctuation_normalize: bool = True
    honorific_strategy: str = "keep_style"
    source_identity: str = ""
    task_groups: tuple[str, ...] = ()
    bilingual: bool = False
    order: Literal["target_first", "source_first"] = "target_first"
    preserve_source_style: bool = False
    about_page: bool = True


@dataclass(frozen=True)
class ResolvedOperation:
    id: str
    point: Point
    implementation_version: str
    contract_version: int
    options: tuple[tuple[str, str], ...]
    origins: tuple[str, ...]
    enabled: bool
    reason: str
    llm_operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportOptions:
    language_tag: str
    about_locale: str = "en"
    target_font: str | None = None
    preserve_source_ruby: bool = False


@dataclass(frozen=True)
class PolicyPlan:
    context: PolicyContext
    selections: tuple[ResolvedOperation, ...]
    prompt_values: tuple[tuple[str, str], ...]
    templates: tuple[tuple[str, str], ...]
    resources: tuple[tuple[str, str], ...]
    export: ExportOptions
    fingerprint: str
    semantic_fingerprint: str

    @property
    def operations(self) -> tuple[ResolvedOperation, ...]:
        return tuple(op for op in self.selections if op.enabled)

    def enabled(self, operation: str) -> bool:
        return any(op.id == operation and op.enabled for op in self.selections)

    def selection(self, operation: str) -> ResolvedOperation:
        return next(op for op in self.selections if op.id == operation)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}

    def preview(self) -> dict[str, Any]:
        """Expose decisions and identity without embedding complete prompt templates."""
        return {
            "fingerprint": self.fingerprint,
            "context": asdict(self.context),
            "semantic_fingerprint": self.semantic_fingerprint,
            "selections": [asdict(op) for op in self.selections],
            "resources": dict(self.resources),
            "export": asdict(self.export),
            "model_operations": sorted(
                {model for op in self.operations for model in op.llm_operations}
            ),
            "may_require_model_calls": self.context.phase != "export",
        }

    def task_fingerprint(self, prefix: str) -> str:
        """Bind a derived artifact only to the templates and rules that produced it."""
        templates = tuple(
            (name, text) for name, text in self.templates if name.startswith(prefix + "_")
        )
        used = set(re.findall(r"\$\{?(\w+)", "\n".join(text for _, text in templates)))
        values = {key: value for key, value in self.prompt_values if key in used}
        if prefix in {"translator", "review_fixer"} and "lang_guidance" in used:
            values["lang_guidance"] = dict(self.prompt_values)["configured_lang_guidance"]
        return content_hash(
            {
                "source": self.context.source,
                "target": self.context.target,
                "source_identity": self.context.source_identity,
                "path": self.context.path,
                "templates": templates,
                "rules": values,
                "operations": [
                    (op.id, op.implementation_version, op.contract_version, op.options)
                    for op in self.operations
                    if op.point == "prompt.compose"
                ],
            }
        )


@dataclass(frozen=True)
class ExportTextInput:
    segment_ids: tuple[int, ...]
    targets: tuple[str | None, ...]
    continuations: tuple[bool, ...]
    logical_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ExportTextResult:
    segment_ids: tuple[int, ...]
    targets: tuple[str | None, ...]
    continuations: tuple[bool, ...]
    logical_ranges: tuple[tuple[int, int], ...]
    logical_boundary_maps: tuple[tuple[int, ...], ...]
