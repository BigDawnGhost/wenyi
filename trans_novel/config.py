from __future__ import annotations

import os
from typing import Any

import yaml
from pydantic import BaseModel, Field


class TierConfig(BaseModel):
    model: str
    reasoning_effort: str = "high"
    thinking: bool = True
    extra_body: dict[str, Any] | None = None


class LLMConfig(BaseModel):
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    timeout: int = 600
    max_retries: int = 4
    tiers: dict[str, TierConfig] = Field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        if os.environ.get("LLM_API_KEY"):
            return os.environ.get("LLM_API_KEY")
        val = os.environ.get(self.api_key_env)
        if val:
            return val
        return os.environ.get("OPENAI_API_KEY") or os.environ.get("API_KEY")


class SegmentConfig(BaseModel):
    max_chars_per_batch: int = 1800
    max_chars_per_segment: int = 1200


class PipelineConfig(BaseModel):
    review: bool = True
    autofix_severe: bool = True
    align_retry_limit: int = 2
    polish: bool = False
    backtranslate_sample: float = 0.05
    consistency_qa: bool = True
    rolling_context_segments: int = 6
    book_understanding: bool = True
    prescan_concurrency: int = 4
    glossary_scope: str = "chapter"


class Config(BaseModel):
    source_lang: str = "auto"
    target_lang: str = "zh"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    segment: SegmentConfig = Field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    honorific_strategy: str = "keep_style"
    punctuation_normalize: bool = True
    state_dir: str = "state"

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass

        raw = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        lang = raw.get("language", {}) or {}
        llm_raw = raw.get("llm", {}) or {}
        
        provider = os.environ.get("LLM_PROVIDER") or llm_raw.get("provider", "openai")
        base_url = os.environ.get("LLM_BASE_URL") or llm_raw.get("base_url", "https://api.openai.com/v1")
        api_key_env = os.environ.get("LLM_API_KEY_ENV") or llm_raw.get("api_key_env", "OPENAI_API_KEY")
        
        try:
            timeout = int(os.environ.get("LLM_TIMEOUT") or llm_raw.get("timeout", 600))
        except ValueError:
            timeout = 600
            
        try:
            max_retries = int(os.environ.get("LLM_MAX_RETRIES") or llm_raw.get("max_retries", 4))
        except ValueError:
            max_retries = 4

        raw_tiers = llm_raw.get("tiers", {}) or {}
        if not raw_tiers:
            raw_tiers = {
                "strong": {"model": "gpt-4o", "reasoning_effort": "high", "thinking": True},
                "cheap": {"model": "gpt-4o-mini", "reasoning_effort": "high", "thinking": True},
                "fast": {"model": "gpt-4o-mini", "thinking": False},
            }
            
        tiers = {}
        for name, t in raw_tiers.items():
            t_obj = TierConfig.model_validate(t)
            env_model = os.environ.get(f"LLM_MODEL_{name.upper()}")
            if env_model:
                t_obj.model = env_model
            tiers[name] = t_obj

        llm = LLMConfig(
            provider=provider,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout=timeout,
            max_retries=max_retries,
            tiers=tiers,
        )
        segment = SegmentConfig.model_validate(raw.get("segment", {}) or {})
        pipeline = PipelineConfig.model_validate(raw.get("pipeline", {}) or {})
        punct = raw.get("punctuation", {}) or {}
        return cls(
            source_lang=lang.get("source", "auto"),
            target_lang=lang.get("target", "zh"),
            llm=llm,
            segment=segment,
            pipeline=pipeline,
            honorific_strategy=raw.get("honorific", {}).get("strategy", "keep_style"),
            punctuation_normalize=bool(punct.get("normalize", True)),
            state_dir=raw.get("paths", {}).get("state_dir", "state"),
        )
