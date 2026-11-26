"""Load config.yaml with typed defaults using Pydantic v2."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .i18n.genres import DEFAULT_GENRE, Genre, require_genre
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
  preset: deepseek # Registers deepseek-flash for each operation; glossary.extract uses low thinking.
  # Select a registered connection/model for each operation with routes.<operation>.model.
  # Optionally override its thinking effort with routes.<operation>.thinking.
  # Budget and quota estimates divide prompt bytes by 2.0 by default; set
  # budget.token_estimate_divisor to 1.0 to keep the previous byte estimate.
  # Inspect effective settings with: wenyi models list

# ── Segmentation ─────────────────────────────────────────────────────────────────
segment:
  # Target batch size in tokens (tiktoken cl100k_base).
  max_tokens_per_batch: 1800
  # Split longer paragraphs at sentence boundaries by the same token budget; merge on export.
  max_tokens_per_segment: 1200

# ── Pipeline options (quality and cost)───────────────────────────────────────────
pipeline:
  # all: body batches get the full glossary (book-wide consistency, larger prompts).
  # relevant: each batch gets only entries matching its source, next source, annotations
  # and chapter digest; glossary extraction then sees only entries present in its batch.
  # Reviewer requests always use the full glossary.
  batch_glossary: all # all | relevant
  # incremental: extract glossary entries after each batch.
  # compiled: compile a frozen Book Bible upfront from digests + dual-context entropy probe,
  # skipping hot-path per-batch extraction.
  glossary_mode: incremental # incremental | compiled
  # Optional glossary token cap per request (cl100k_base); 0 means no cap, so batch_glossary
  # alone decides what is sent. With a cap, all mode keeps the append-stable leading run within
  # it plus matching later entries, and relevant mode caps matched entries.
  glossary_prompt_tokens: 0
  # Optional ingest-time filter for domain promo watermarks, ad-network markers and lone >/| lines;
  # add boilerplate_patterns for site-specific debris.
  skip_boilerplate: false
  boilerplate_patterns: []
  review: true # Run final review after whole-book translation; disable with --no-review
  # Additional structural retries for standard translation and each precision stage; 0 disables retries.
  align_retry_limit: 2
  polish: true # Polish the full translation using polish.body; enabled by default and adds substantial cost
  # full: rewrite every batch (default). sparse: source-grounded editor patching only defective paragraphs.
  polish_mode: full # full | sparse
  translation_mode: standard # standard | three_draft_synthesis; three_draft_synthesis requires polish
  # Concurrent chapter drafting workers; 1 translates chapters serially with cross-chapter target context.
  # Above 1, chapters translate concurrently with deterministic source lookbehind and a single storage writer.
  chapter_concurrency: 1
  # Standard-mode polish workers; above 1, polishing runs in the background while translation continues.
  polish_concurrency: 1
  # Zero-cost post-translation repair of leaked newline escapes; never rewrites punctuation or names.
  deterministic_hygiene: true
  # Fast post-translation cross-chapter sweep: patches missed glossary terms; title drift needs compiled mode.
  consistency_sweep: false
  rolling_context_tokens: 512 # Recent translation budget in tokens (tiktoken cl100k_base)
  glossary_note_workers: 0 # Incremental evidence notes: 0 disables, 1-16 workers per batch
  book_understanding: true # Prescan complete chapters for digests and a whole-book synopsis; disable to save prescan cost
  prescan_concurrency: 4 # Concurrent prescan workers for chapter digests and book-synopsis reduction; 1 runs serially
  # Prescan content budgets in cl100k_base tokens; prompt/context overhead is additional.
  # Larger values reduce splitting but may exceed model limits or reduce summary quality.
  synopsis_source_tokens: 8000 # Maximum source tokens per chapter-digest chunk
  synopsis_reduce_tokens: 12000 # Maximum joined digest tokens per merge, generated summary, and style-analysis digest block
  annotation_alignment: true # Align translated annotations; EPUB uses fallback markers, PDF omits failed items
  annotation_alignment_concurrency: 4 # Maximum concurrent per-annotation requests for a logical paragraph with multiple annotations
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

# ── Paths ─────────────────────────────────────────────────────────────────
paths:
  state_dir: state # Run state, intermediate chapter files and glossary

# ── Output ───────────────────────────────────────────────────────────────────
output:
  mono: true # Monolingual output (<title>.<target-language>.epub; default target is zh)
  bilingual: false # Bilingual output (<title>.<target-language>-bi.epub)
  bilingual_order: target_first # target_first=translation first; source_first=source first
  # PDF places the first version on the left; other formats use paragraph order.
  # true=preserve source styles; false=muted source in EPUB/HTML, normal body text in DOCX
  bilingual_preserve_source_style: false
  about_page: true # Append an About This Translation page
  quote_style: preserve # preserve | typographic (English targets) | straight (Latin-script targets); export-only
"""


class SegmentConfig(BaseModel):
    """Source packing budgets measured with tiktoken ``cl100k_base``."""

    model_config = ConfigDict(extra="forbid")

    # gt=0 on the batch budget: a persisted 0 would degrade packing to one request per
    # segment with no error anywhere (audit Y-3/F-2). max_tokens_per_segment keeps 0 as
    # a valid knob — the splitter then splits every sentence into its own segment.
    max_tokens_per_batch: int = Field(default=1800, gt=0)
    max_tokens_per_segment: int = Field(default=1200, ge=0)


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translation_mode: Literal["standard", "three_draft_synthesis"] = "standard"

    @model_validator(mode="after")
    def validate_translation_mode(self) -> PipelineConfig:
        if self.translation_mode == "three_draft_synthesis" and not self.polish:
            raise ValueError("three_draft_synthesis translation mode requires pipeline.polish=true")
        return self

    @model_validator(mode="before")
    @classmethod
    def retire_terminology_context(cls, raw: Any) -> Any:
        # Older default YAML explicitly disabled the removed pretranslation experiment.
        if isinstance(raw, dict) and "terminology_context" in raw:
            if raw["terminology_context"] is not False:
                raise ValueError(
                    "pipeline.terminology_context has been removed; use glossary_note_workers for incremental notes"
                )
            return {key: value for key, value in raw.items() if key != "terminology_context"}
        return raw

    # Glossary lifecycle: "incremental" extracts per batch; "compiled" builds a frozen Book Bible
    # from digests + dual-context entropy probing before chapter 1 and skips hot-path extraction.
    glossary_mode: Literal["incremental", "compiled"] = "incremental"
    # Glossary sent to body translation, polish, precision and extraction requests:
    # "all" keeps book-wide consistency; "relevant" sends only entries matching each batch.
    batch_glossary: Literal["all", "relevant"] = "all"
    # Optional per-request glossary token cap (cl100k_base); 0 means no cap.
    glossary_prompt_tokens: int = Field(default=0, ge=0)
    # Optional ingest-time scraped-site boilerplate filter (off by default).
    skip_boilerplate: bool = False
    boilerplate_patterns: list[str] = Field(default_factory=list)

    @field_validator("boilerplate_patterns")
    @classmethod
    def validate_boilerplate_patterns(cls, value: list[str]) -> list[str]:
        import re

        for index, raw in enumerate(value):
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(
                    f"pipeline.boilerplate_patterns[{index}] must be a non-empty regular expression"
                )
            try:
                re.compile(raw)
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex in pipeline.boilerplate_patterns[{index}] ({raw!r}): {exc}"
                ) from exc
        return value

    review: bool = True
    # Additional structural retries for standard translation and precision generation/synthesis.
    align_retry_limit: int = 2
    polish: bool = True  # Polish the full translation by default; disable to save cost
    # "full" rewrites every batch; "sparse" runs a source-grounded semantic editor patching only defects.
    polish_mode: Literal["full", "sparse"] = "full"
    # Concurrent chapter drafting workers; >1 uses deterministic source lookbehind and a single writer.
    chapter_concurrency: int = Field(default=1, ge=1, le=16)
    # Above 1, standard-mode polish jobs run in a bounded background pool while translation
    # continues; 1 polishes each batch inline. Ignored by three_draft_synthesis.
    polish_concurrency: int = Field(default=1, ge=1)
    # Repair leaked newline escapes (no model calls); punctuation and names are never rewritten.
    deterministic_hygiene: bool = True
    # Fast cross-chapter sweep: missed glossary terms, plus Book Bible title drift in compiled mode.
    consistency_sweep: bool = False
    rolling_context_tokens: int = Field(default=512, ge=0)
    # Note generation requires an opted-in workflow runner.
    glossary_note_workers: int = Field(default=0, ge=0, le=16)

    @property
    def use_glossary_notes(self) -> bool:
        return self.glossary_note_workers > 0

    # Prescan for a synopsis and chapter digests; disable to save prescan cost.
    book_understanding: bool = True
    prescan_concurrency: int = 4  # Concurrent prescan workers for chapter digests and book-synopsis reduction; 1 runs serially
    # Prescan content budgets in cl100k_base tokens, excluding prompt/context overhead.
    synopsis_source_tokens: int = Field(default=8000, gt=0)
    synopsis_reduce_tokens: int = Field(default=12000, gt=0)
    annotation_alignment: bool = (
        True  # Align annotations after each logical paragraph; PDF omits failed items
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
    model_config = ConfigDict(extra="forbid")

    mono: bool = True  # Generate monolingual output
    bilingual: bool = False  # Generate bilingual output
    bilingual_order: Literal["target_first", "source_first"] = (
        "target_first"  # target_first=translation first (default); source_first=source first
    )
    bilingual_preserve_source_style: bool = False  # EPUB/HTML styling and DOCX source metadata
    about_page: bool = True  # Append the project about page
    quote_style: Literal["preserve", "typographic", "straight"] = "preserve"


class LanguageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "auto"
    target: str = "zh"

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return require_language(value, allow_auto=True)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return require_language(value)


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_dir: str = "state"


CONFIG_SECTIONS: dict[str, type[BaseModel]] = {
    "language": LanguageConfig,
    "llm": LLMConfig,
    "segment": SegmentConfig,
    "pipeline": PipelineConfig,
    "output": OutputConfig,
    "paths": PathsConfig,
}


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_lang: str = "auto"  # auto | ja | en | … (auto uses model detection)
    target_lang: str = "zh"
    _language_plans: dict[str, PolicyPlan] = PrivateAttr(default_factory=dict)
    _prompt_genre: Genre = PrivateAttr(default=DEFAULT_GENRE)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    segment: SegmentConfig = Field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    state_dir: str = "state"

    @model_validator(mode="after")
    def validate_builtin_language_policy(self) -> Config:
        resolve_policy(PolicyContext(self.source_lang, self.target_lang))
        return self

    @property
    def prompt_genre(self) -> Genre:
        """Return the runtime prompt family without adding a Core YAML setting."""
        return self._prompt_genre

    def set_prompt_genre(self, genre: str) -> None:
        """Select a runtime prompt family before constructing agents; keep Core YAML unchanged."""
        self._prompt_genre = require_genre(genre)
        self._language_plans.clear()

    def set_experimental_prompt_genre(self, genre: str) -> None:
        """Keep the original experimental API as an alias for existing callers."""
        self.set_prompt_genre(genre)

    def language_policy(
        self,
        phase: Phase = "translation",
        *,
        format: str = "",
        backend: str = "native",
        source_identity: str = "",
    ) -> PolicyPlan:
        """Reuse the invocation's frozen semantic plan; exports resolve their snapshot."""
        key = f"book:{phase}"
        if phase != "export" and key in self._language_plans:
            return self._language_plans[key]
        context = PolicyContext(
            self.source_lang,
            self.target_lang,
            phase=phase,
            format=format,
            backend=backend,
            source_identity=source_identity,
            task_groups=self._policy_tasks(phase),
            bilingual=self.output.bilingual,
            order=self.output.bilingual_order,
            preserve_source_style=self.output.bilingual_preserve_source_style,
            about_page=self.output.about_page,
            genre=self._prompt_genre,
        )
        return resolve_policy(context)

    def _policy_tasks(self, phase: Phase) -> tuple[str, ...]:
        if phase == "analysis":
            return (
                ("analyzer", "chapter_digest", "book_synopsis")
                if self.pipeline.book_understanding
                else ("analyzer",)
            )
        if phase == "translation":
            notes = ("glossary_note",) if self.pipeline.glossary_note_workers else ()
            if self.pipeline.translation_mode == "three_draft_synthesis":
                return (
                    "precision",
                    "title_translator",
                    "glossary_extractor",
                    "glossary_history",
                ) + notes
            return (
                ("translator", "title_translator", "glossary_extractor", "glossary_history")
                + (("polisher",) if self.pipeline.polish else ())
                + notes
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

    def language_policy_revision(self) -> str:
        """Describe the built-in semantic revision without source-content hashes."""
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
    ) -> dict[str, Any]:
        """Use the execution resolver for diagnostics; auto source remains unresolved."""
        phases = ("analysis", "translation", "review")
        result = {phase: self.language_policy(phase).preview() for phase in phases}
        result["revision"] = {"fingerprint": self.language_policy_revision()}
        from .llm.operations import configured_operations
        from .llm.routing import resolve_routes

        routes = resolve_routes(self.llm)
        for phase in phases:
            workflow = (
                "prepare" if phase == "analysis" else "review" if phase == "review" else "translate"
            )
            operations = configured_operations(self, workflow)
            if phase == "translation":
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
                "export", format=format, backend=backend
            ).preview()
        else:
            formats = ("epub", "docx", "html", "txt", "markdown", "pdf")
            exports = {}
            for selected in formats:
                try:
                    exports[selected] = self.language_policy(
                        "export", format=selected, backend=backend
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
            raw = parse_config_yaml(f.read())
        return cls.from_dict({} if raw is None else raw)

    @classmethod
    def from_dict(cls, raw: Any) -> Config:
        """Convert a nested YAML dictionary into the runtime configuration model."""
        if not isinstance(raw, dict):
            raise ValueError("Configuration must be a mapping of sections.")
        unknown = set(raw) - CONFIG_SECTIONS.keys()
        if unknown:
            raise ValueError(
                "Unknown configuration sections: " + ", ".join(sorted(map(str, unknown)))
            )
        lang = raw.get("language", {})
        if not isinstance(lang, dict) or set(lang) - {"source", "target"}:
            raise ValueError("Invalid or unknown language configuration fields")
        sections = {name: {} if raw.get(name) is None else raw[name] for name in CONFIG_SECTIONS}
        language = LanguageConfig.model_validate(sections["language"])
        paths = PathsConfig.model_validate(sections["paths"])
        return cls(
            source_lang=language.source,
            target_lang=language.target,
            llm=LLMConfig.model_validate(sections["llm"]),
            segment=SegmentConfig.model_validate(sections["segment"]),
            pipeline=PipelineConfig.model_validate(sections["pipeline"]),
            output=OutputConfig.model_validate(sections["output"]),
            state_dir=paths.state_dir,
        )
