"""Typed source evidence and model results, independent of workflow and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .store import GlossaryTerm


class SourcePassage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    chapter: int
    segment: int
    source: str
    start: int = 0


class KnowledgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = ""
    reading: str = ""
    gender: str = ""
    aliases: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class DiscoveryRejection(BaseModel):
    """Audit a rejected proposal without retaining source names, values or references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection: Literal["terms"]
    index: int = Field(ge=0)
    reason: Literal["invalid_fields", "missing", "outside_input", "source_absent"]


@dataclass
class DiscoveryResult:
    terms: list[GlossaryTerm] = field(default_factory=list)
    rejections: list[DiscoveryRejection] = field(default_factory=list)
