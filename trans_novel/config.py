"""Load config.yaml with typed defaults using Pydantic v2."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .i18n.languages import require_language
from .llm.configuration import LLMConfig

_DEFAULT_CONFIG_YAML = """\
# trans-novel configuration (experimental multilingual fiction translation)
# Configure model providers, workflow stages and output here; no code changes are needed.

language:
  source: auto # auto detects the source language; use an explicit code such as ja / en / ko / ru / de to override
  target: zh # Target: zh / zh-Hant / en / ja / ko / fr / de / es / it / pt / ru; run languages for the full list

# ── LLM ──────────────────────────────────────────────────────────────────
llm:
  preset: deepseek # All tiers: deepseek-flash, thinking enabled, reasoning_effort high
  # Add providers, models and routes to override individual operations.
  # Inspect effective settings with: trans-novel models list

# ── Segmentation ─────────────────────────────────────────────────────────────────
segment:
  # Target batch size in characters, used as a rough token estimate.
  max_chars_per_batch: 1800
  # Split longer paragraphs at sentence boundaries; merge continuations back during export.
  max_chars_per_segment: 1200

# ── Pipeline options (quality and cost)───────────────────────────────────────────
pipeline:
  review: true # Run final review after whole-book translation; disable with --no-review
  align_retry_limit: 2
  polish: true # Polish the full translation with the strong tier; enabled by default and adds substantial cost
  rolling_context_segments: 6 # Number of recent translated paragraphs supplied as context
  book_understanding: true # Prescan the source for a whole-book synopsis and chapter digests used during translation
  prescan_concurrency: 4 # Concurrent chapter-digest workers; chapters are independent, 1 runs serially
  annotation_alignment: true # Align EPUB annotation links per paragraph; if disabled, target links fall back to paragraph ends
  annotation_alignment_concurrency: 4 # Maximum concurrent alignment requests when a paragraph has multiple annotations
  review_concurrency: 4 # Concurrent review blocks over a read-only translation/glossary snapshot; 1 runs serially
  review_output_retries: 2 # Additional retries for malformed single-paragraph review output; 2 allows 3 attempts total
  review_agent_loop: true # Use evidence-based verification after the initial review identifies candidates
  review_agent_max_evidence_rounds: 2 # At most two rounds of selective evidence requests before a final decision
  review_conflict_arbitration: true # Arbitrate contradictory consistency proposals after all review blocks finish
  review_fix_loop: true # Revise an in-memory shadow translation and review it blindly; this loop does not publish changes
  review_fix_max_rounds: 2 # At most two replacement rounds; consecutive clean confirmations also affect total review rounds
  review_clean_confirmations: 2 # Require two consecutive clean rounds to accept the shadow translation
  review_autofix: true # Publish review revisions to formal chapters; use --no-autofix for recommendations only
  glossary_scope: chapter # chapter=terms relevant to this chapter; full=entire glossary
  # PDF backend: mineru (default, supports scans) | babeldoc (optional, preserves layout via external AGPL HTTP bridge)
  pdf_backend: mineru
  babeldoc_bridge_url: http://127.0.0.1:8765
  # babeldoc_pages: "15"   # Optional page restriction for the bridge (one-based)
  babeldoc_timeout: 600

# ── Honorific strategy (language-specific rules apply where available)────────────────────
honorific:
  # keep_style: preserve relationship and tone; normalize: apply consistent conventions; drop: omit where meaning permits
  strategy: keep_style

# ── Paths ─────────────────────────────────────────────────────────────────
paths:
  state_dir: state # Run state, intermediate chapter files and glossary

# ── Output ───────────────────────────────────────────────────────────────────
output:
  mono: true # Monolingual output (<title>.<target-language>.epub; default target is zh)
  bilingual: false # Bilingual output (<title>.<target-language>-bi.epub)
  bilingual_order: target_first # target_first=translation first; source_first=source first
  bilingual_preserve_source_style: false # true=preserve original source styling; false=render source in muted gray
  about_page: true # Append an About This Translation page
  punctuation_normalize: true # Normalize only exported copies; preserve formal translation state
"""


class SegmentConfig(BaseModel):
    max_chars_per_batch: int = 1800
    max_chars_per_segment: int = 1200


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: bool = True
    align_retry_limit: int = (
        2  # Retry misaligned batches this many times before falling back to single paragraphs
    )
    polish: bool = (
        True  # Polish the full translation with the strong tier by default; disable to save cost
    )
    rolling_context_segments: int = 6
    # Prescan for a synopsis and chapter digests; disable to save prescan cost.
    book_understanding: bool = True
    prescan_concurrency: int = (
        4  # Concurrent chapter-digest workers; chapters are independent, 1 runs serially
    )
    annotation_alignment: bool = (
        True  # Align links after each annotated logical paragraph is finalized
    )
    # For multiple annotations in a logical paragraph, align each with a concurrent request.
    # This limit bounds concurrency and avoids all markers falling back after one bad response.
    annotation_alignment_concurrency: int = 4
    review_concurrency: int = (
        4  # Concurrent review blocks; merge in original order, 1 runs serially
    )
    review_output_retries: int = Field(
        default=2,
        ge=0,
        le=5,
    )  # Additional retries for malformed single-paragraph output
    review_agent_loop: bool = (
        True  # Start the bounded evidence agent loop when initial review finds candidates
    )
    review_agent_max_evidence_rounds: int = Field(
        default=2,
        ge=0,
        le=2,
    )
    review_conflict_arbitration: bool = (
        True  # Arbitrate contradictory consistency proposals after all blocks finish
    )
    review_fix_loop: bool = (
        True  # Revise only the in-memory shadow translation and review it blindly
    )
    review_fix_max_rounds: int = Field(default=2, ge=0, le=4)
    review_clean_confirmations: int = Field(default=2, ge=1, le=2)
    review_autofix: bool = True  # Publish formal translations through a separate stage after review
    glossary_scope: str = (
        "chapter"  # chapter=terms occurring in this chapter (saves tokens); full=entire glossary
    )
    # PDF: mineru=HTML path for scans (default); babeldoc=external AGPL HTTP bridge (no imports)
    pdf_backend: Literal["mineru", "babeldoc"] = "mineru"
    babeldoc_bridge_url: str = "http://127.0.0.1:8765"
    babeldoc_pages: str | None = None  # For example "15" / "6-8"; None=whole book
    babeldoc_timeout: float = 600.0


class OutputConfig(BaseModel):
    mono: bool = True  # Generate monolingual output
    bilingual: bool = False  # Generate bilingual output
    bilingual_order: str = (
        "target_first"  # target_first=translation first (default); source_first=source first
    )
    bilingual_preserve_source_style: bool = False
    about_page: bool = True  # Append the project about page
    punctuation_normalize: bool = (
        True  # Normalize export copies only; never write back to chapter target
    )


class Config(BaseModel):
    source_lang: str = "auto"  # auto | ja | en | … (auto uses model detection)
    target_lang: str = "zh"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    segment: SegmentConfig = Field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    honorific_strategy: str = "keep_style"
    state_dir: str = "state"

    @field_validator("source_lang")
    @classmethod
    def validate_source_lang(cls, value: str) -> str:
        return require_language(value, allow_auto=True)

    @field_validator("target_lang")
    @classmethod
    def validate_target_lang(cls, value: str) -> str:
        return require_language(value)

    @staticmethod
    def create_default_file(path: str) -> bool:
        """Atomically create the default config if absent; return whether it was created."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as f:
                f.write(_DEFAULT_CONFIG_YAML)
            return True
        except FileExistsError:
            return False

    @classmethod
    def load(cls, path: str = "config.yaml") -> Config:
        """Load YAML configuration and apply typed defaults for missing fields."""
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> Config:
        """Convert a nested YAML dictionary into the runtime configuration model."""
        if not isinstance(raw, dict):
            raise ValueError("Configuration must be a mapping of sections.")
        sections = {"language", "llm", "segment", "pipeline", "output", "honorific", "paths"}
        unknown = set(raw) - sections
        if unknown:
            raise ValueError(
                "Unknown configuration sections: " + ", ".join(sorted(map(str, unknown)))
            )
        lang = raw.get("language", {})
        llm_raw = raw.get("llm", {})
        llm = LLMConfig.model_validate({} if llm_raw is None else llm_raw)
        segment = SegmentConfig.model_validate(raw.get("segment", {}) or {})
        pipeline = PipelineConfig.model_validate(raw.get("pipeline", {}) or {})
        output = OutputConfig.model_validate(raw.get("output", {}) or {})
        return cls(
            source_lang=lang.get("source", "auto"),
            target_lang=lang.get("target", "zh"),
            llm=llm,
            segment=segment,
            pipeline=pipeline,
            output=output,
            honorific_strategy=raw.get("honorific", {}).get("strategy", "keep_style"),
            state_dir=raw.get("paths", {}).get("state_dir", "state"),
        )
