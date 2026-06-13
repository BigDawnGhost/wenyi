"""配置加载。读取 config.yaml，提供带默认值的类型化访问。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class TierConfig:
    model: str
    reasoning_effort: str = "high"
    thinking: bool = True


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout: int = 600
    max_retries: int = 4
    tiers: dict[str, TierConfig] = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


@dataclass
class SegmentConfig:
    max_chars_per_batch: int = 1800
    max_chars_per_segment: int = 1200


@dataclass
class PipelineConfig:
    review: bool = True
    review_retry_limit: int = 2
    polish: bool = True
    backtranslate_sample: float = 0.05
    consistency_qa: bool = True
    rolling_context_segments: int = 6


@dataclass
class Config:
    source_lang: str = "ja"
    target_lang: str = "zh"
    llm: LLMConfig = field(default_factory=LLMConfig)
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    honorific_strategy: str = "keep_style"
    state_dir: str = "state"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        lang = raw.get("language", {})
        llm_raw = raw.get("llm", {})
        tiers = {
            name: TierConfig(
                model=t["model"],
                reasoning_effort=t.get("reasoning_effort", "high"),
                thinking=t.get("thinking", True),
            )
            for name, t in (llm_raw.get("tiers", {}) or {}).items()
        }
        llm = LLMConfig(
            provider=llm_raw.get("provider", "deepseek"),
            base_url=llm_raw.get("base_url", "https://api.deepseek.com"),
            api_key_env=llm_raw.get("api_key_env", "DEEPSEEK_API_KEY"),
            timeout=llm_raw.get("timeout", 600),
            max_retries=llm_raw.get("max_retries", 4),
            tiers=tiers,
        )
        seg_raw = raw.get("segment", {})
        segment = SegmentConfig(
            max_chars_per_batch=seg_raw.get("max_chars_per_batch", 1800),
            max_chars_per_segment=seg_raw.get("max_chars_per_segment", 1200),
        )
        pipe_raw = raw.get("pipeline", {})
        pipeline = PipelineConfig(
            review=pipe_raw.get("review", True),
            review_retry_limit=pipe_raw.get("review_retry_limit", 2),
            polish=pipe_raw.get("polish", True),
            backtranslate_sample=pipe_raw.get("backtranslate_sample", 0.05),
            consistency_qa=pipe_raw.get("consistency_qa", True),
            rolling_context_segments=pipe_raw.get("rolling_context_segments", 6),
        )
        return cls(
            source_lang=lang.get("source", "ja"),
            target_lang=lang.get("target", "zh"),
            llm=llm,
            segment=segment,
            pipeline=pipeline,
            honorific_strategy=raw.get("honorific", {}).get("strategy", "keep_style"),
            state_dir=raw.get("paths", {}).get("state_dir", "state"),
            raw=raw,
        )
