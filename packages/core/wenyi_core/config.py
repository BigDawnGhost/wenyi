"""Load config.yaml with typed defaults using Pydantic v2."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .i18n.languages import require_language
from .i18n.policy.models import Phase, PolicyContext, PolicyPlan, content_hash
from .i18n.policy.resolver import resolve_policy
from .llm.configuration import LLMConfig


def parse_config_yaml(text: str) -> Any:
    """Parse CLI and Web configuration with the same safe scalar semantics."""
    return yaml.safe_load(text)


_DEFAULT_CONFIG_YAML = """\
# Wenyi configuration (experimental multilingual fiction translation)
# Configure model providers, workflow stages and output here; no code changes are needed.

language:
  source: auto # auto detects the source language; use an explicit code such as ja / en / ko / ru / de / vi to override
  target: zh # Target: zh / zh-Hant / en / ja / ko / fr / de / es / it / pt / ru / vi; run languages for the full list

# ── LLM ──────────────────────────────────────────────────────────────────
llm:
  preset: deepseek # All tiers: deepseek-flash, thinking enabled, reasoning_effort high
  # Choose a provider and its tier models interactively with: wenyi model
  # Add providers, models and routes to override individual operations.
  # List provider kinds and aliases with: wenyi models providers
  # Inspect effective settings with: wenyi models list

# ── Segmentation ─────────────────────────────────────────────────────────────────
segment:
  # Target batch size in tokens (tiktoken cl100k_base).
  max_tokens_per_batch: 1800
  # Split longer paragraphs at sentence boundaries by the same token budget; merge on export.
  max_tokens_per_segment: 1200

# ── Pipeline options (quality and cost)───────────────────────────────────────────
pipeline:
  # Body translation and Reviewer requests always use the full glossary.
  review: true # Run final review after whole-book translation; disable with --no-review
  align_retry_limit: 2
  polish: true # Polish the full translation with the strong tier; enabled by default and adds substantial cost
  translation_mode: standard # standard | best_of_three; best_of_three requires polish
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
    """Source packing budgets measured with tiktoken ``cl100k_base``."""

    model_config = ConfigDict(extra="forbid")

    max_tokens_per_batch: int = 1800
    max_tokens_per_segment: int = 1200


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translation_mode: Literal["standard", "best_of_three"] = "standard"

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_precision_concurrency(cls, value: Any) -> Any:
        """Read existing configurations and job snapshots without exposing a retired option."""
        if isinstance(value, dict) and "precision_concurrency" in value:
            return {key: item for key, item in value.items() if key != "precision_concurrency"}
        return value

    @model_validator(mode="after")
    def validate_translation_mode(self) -> PipelineConfig:
        if self.translation_mode == "best_of_three" and not self.polish:
            raise ValueError("best_of_three translation mode requires pipeline.polish=true")
        return self

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
    # PDF: mineru=HTML path for scans (default); babeldoc=external AGPL HTTP bridge (no imports)
    pdf_backend: Literal["mineru", "babeldoc"] = "mineru"
    babeldoc_bridge_url: str = "http://127.0.0.1:8765"
    babeldoc_pages: str | None = None  # For example "15" / "6-8"; None=whole book
    babeldoc_timeout: float = 600.0


class OutputConfig(BaseModel):
    mono: bool = True  # Generate monolingual output
    bilingual: bool = False  # Generate bilingual output
    bilingual_order: Literal["target_first", "source_first"] = (
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
    _language_plans: dict[str, PolicyPlan] = PrivateAttr(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    segment: SegmentConfig = Field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    honorific_strategy: str = "keep_style"
    state_dir: str = "state"

    @model_validator(mode="after")
    def validate_builtin_language_policy(self) -> Config:
        resolve_policy(
            PolicyContext(
                self.source_lang,
                self.target_lang,
                punctuation_normalize=self.output.punctuation_normalize,
            ),
        )
        return self

    def language_policy(
        self,
        phase: Phase = "translation",
        *,
        path: Literal["book", "srt"] = "book",
        format: str = "",
        backend: str = "native",
        source_identity: str = "",
    ) -> PolicyPlan:
        """Reuse the invocation's frozen semantic plan; exports resolve their snapshot."""
        key = f"{path}:{phase}"
        if phase != "export" and key in self._language_plans:
            return self._language_plans[key]
        context = PolicyContext(
            self.source_lang,
            self.target_lang,
            phase=phase,
            path=path,
            format=format,
            backend=backend,
            punctuation_normalize=self.output.punctuation_normalize,
            honorific_strategy=self.honorific_strategy,
            source_identity=source_identity,
            task_groups=self._policy_tasks(phase, path),
            bilingual=self.output.bilingual,
            order=self.output.bilingual_order,
            preserve_source_style=self.output.bilingual_preserve_source_style,
            about_page=self.output.about_page,
        )
        return resolve_policy(context)

    def _policy_tasks(self, phase: Phase, path: str) -> tuple[str, ...]:
        if path == "srt":
            return ("srt_batch", "srt_single")
        if phase == "analysis":
            return (
                ("analyzer", "chapter_digest", "book_synopsis")
                if self.pipeline.book_understanding
                else ("analyzer",)
            )
        if phase == "translation":
            if self.pipeline.translation_mode == "best_of_three":
                return ("precision", "title_translator", "glossary_extractor", "glossary_history")
            return ("translator", "title_translator", "glossary_extractor", "glossary_history") + (
                ("polisher",) if self.pipeline.polish else ()
            )
        if phase == "review":
            groups = ["reviewer"]
            if self.pipeline.review_agent_loop or self.pipeline.review_autofix:
                groups.append("review_agent")
            if self.pipeline.review_agent_loop and self.pipeline.review_conflict_arbitration:
                groups.append("review_arbiter")
            if self.pipeline.review_fix_loop or self.pipeline.review_autofix:
                groups.append("review_fixer")
            return tuple(groups)
        return ()

    def freeze_language_policies(self, source_identity: str = "") -> None:
        """Freeze resources after source detection, before any consuming model call."""
        self._language_plans.clear()
        plans = {
            phase: self.language_policy(phase, source_identity=source_identity)
            for phase in ("analysis", "translation", "review")
        }
        self._language_plans.update({f"book:{phase}": plan for phase, plan in plans.items()})

    def language_document(self) -> dict[str, Any]:
        return {
            "source": self.source_lang,
            "target": self.target_lang,
        }

    def language_policy_revision(self, *, path: Literal["book", "srt"] = "book") -> str:
        """Describe the built-in semantic revision without source-content hashes."""
        if path == "srt":
            return self.language_policy("translation", path=path).fingerprint
        return content_hash(
            {
                phase: self.language_policy(phase).semantic_fingerprint
                for phase in ("analysis", "translation")
            }
        )

    def language_policy_preview(
        self,
        *,
        format: str | None = None,
        backend: str = "native",
        path: Literal["book", "srt"] = "book",
    ) -> dict[str, Any]:
        """Use the execution resolver for diagnostics; auto source remains unresolved."""
        phases = ("translation",) if path == "srt" else ("analysis", "translation", "review")
        result = {phase: self.language_policy(phase, path=path).preview() for phase in phases}
        result["revision"] = {"fingerprint": self.language_policy_revision(path=path)}
        from .llm.operations import configured_operations
        from .llm.routing import resolve_routes

        routes = resolve_routes(self.llm)
        for phase in phases:
            workflow = (
                "srt"
                if path == "srt"
                else "prepare"
                if phase == "analysis"
                else "review"
                if phase == "review"
                else "translate"
            )
            operations = configured_operations(self, workflow)
            if phase == "translation" and path == "book":
                operations = tuple(
                    operation
                    for operation in operations
                    if not operation.startswith(
                        ("language.", "analysis.", "synopsis.", "review.", "autofix.")
                    )
                )
            result[phase]["model_routes"] = [
                routes[operation].describe() for operation in operations
            ]
        if format is not None:
            result["export"] = self.language_policy(
                "export", path=path, format=format, backend=backend
            ).preview()
        else:
            formats = (
                ("srt",) if path == "srt" else ("epub", "docx", "html", "txt", "markdown", "pdf")
            )
            exports = {}
            for selected in formats:
                try:
                    exports[selected] = self.language_policy(
                        "export", path=path, format=selected, backend=backend
                    ).preview()
                except ValueError as error:
                    exports[selected] = {"available": False, "reason": str(error)}
            result["export"] = exports
        return result

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
            raw = parse_config_yaml(f.read()) or {}
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
        if not isinstance(lang, dict) or set(lang) - {"source", "target"}:
            raise ValueError("Invalid or unknown language configuration fields")
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


_LLM_MARKER = re.compile(r"^llm[ \t]*:", re.MULTILINE)
_TOP_LEVEL_KEY = re.compile(r"^(?![ \t#])\S")
_GENERATED_LLM_COMMENT = "# Selected with `wenyi model`; edit freely or rerun that command.\n"


def replace_llm_section(text: str, section: Mapping[str, Any]) -> str:
    """Return configuration text with only the top-level ``llm:`` block rewritten.

    Other sections keep their text, comments and ordering. Blank lines and comments directly
    above the next top-level key stay with that key rather than inside the replaced block.
    """
    block = _GENERATED_LLM_COMMENT + yaml.safe_dump(
        {"llm": dict(section)}, sort_keys=False, allow_unicode=True
    )
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if _LLM_MARKER.match(line)), None)
    if start is None:
        separator = "" if not text.strip() else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + block

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _TOP_LEVEL_KEY.match(lines[index]):
            end = index
            break
    end = _replaced_end(lines, start, end)
    tail = "".join(lines[end:])
    separator = "" if not tail or tail.startswith("\n") else "\n"
    return "".join(lines[:start]) + block + separator + tail


def write_llm_section(path: str, section: Mapping[str, Any]) -> None:
    """Validate the resulting configuration, then replace its ``llm:`` block atomically."""
    target = Path(path)
    text = target.read_text(encoding="utf-8") if target.is_file() else _DEFAULT_CONFIG_YAML
    updated = replace_llm_section(text, section)
    Config.from_dict(yaml.safe_load(updated))
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = target.stat().st_mode & 0o777 if target.is_file() else None
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(updated)
    try:
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _replaced_end(lines: list[str], start: int, end: int) -> int:
    """Shrink a block's end so the next section keeps its blank lines and header comment.

    Indented comments belong to the block being replaced; comments at column zero belong to
    the section that follows.
    """
    while end > start + 1:
        line = lines[end - 1]
        if not line.strip():
            end -= 1
            continue
        if line[0].isspace() or not line.lstrip().startswith("#"):
            break
        end -= 1
    return end
