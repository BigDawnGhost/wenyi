"""用户配置与运行时策略。

公开 YAML 表达模型、模型 thinking 级别，以及质量/成本档位。
Agent 路由、重试、切分等实现细节由代码统一管理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from trans_novel.model_profiles import (
    ReasoningEffort,
    parse_provider_model,
    validate_model_selection,
)

PRODUCTION_AGENT_IDS: tuple[str, ...] = (
    "translator",
    "editor",
    "analyst",
    "preparer",
)

QualityPreset = Literal["economy", "balanced", "quality"]

_DEFAULT_TRANSLATOR_MODEL = "openrouter/tencent/hy-mt2-30b-a3b:off"
_DEFAULT_GENERAL_MODEL = "opencode-go/muse-spark-1.3-contributor:low"
_DEPRECATED_ROOT_KEYS = frozenset(
    {"language", "segment", "pipeline", "honorific", "punctuation", "paths"}
)
_DEPRECATED_LLM_KEYS = frozenset({"provider", "providers", "agents", "tiers"})


@dataclass(frozen=True)
class ModelRef:
    """一次已解析模型请求；model 是发送给服务的精确模型 ID。"""

    provider: str
    model: str
    reasoning_enabled: bool = False
    reasoning_effort: ReasoningEffort = "high"

    @property
    def full_name(self) -> str:
        return f"{self.provider}:{self.model}"


class ModelRoles(BaseModel):
    """用户可选的 translator、analyst、editor、fast 四个模型角色。"""

    model_config = ConfigDict(extra="forbid")

    translator: list[str] = Field(default_factory=lambda: [_DEFAULT_TRANSLATOR_MODEL])
    analyst: list[str] = Field(default_factory=lambda: [_DEFAULT_GENERAL_MODEL])
    editor: list[str] = Field(default_factory=lambda: [_DEFAULT_GENERAL_MODEL])
    fast: list[str] = Field(default_factory=lambda: [_DEFAULT_GENERAL_MODEL])

    @field_validator("translator", "analyst", "editor", "fast")
    @classmethod
    def _model_ids_non_empty(cls, value: list[str] | None) -> list[str]:
        if value is None:
            raise ValueError("模型列表不能为空")
        if not value:
            raise ValueError("模型列表不能为空")
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("模型 ID 不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("模型列表不能包含重复候选")
        return normalized


class ProviderRouting(BaseModel):
    """OpenRouter provider constraints for one configured model."""

    model_config = ConfigDict(extra="forbid")

    only: list[str] | None = Field(default=None, min_length=1)
    order: list[str] | None = Field(default=None, min_length=1)
    allow_fallbacks: bool | None = None


class LLMConfig(BaseModel):
    """四模型角色的公开 LLM 配置。"""

    model_config = ConfigDict(extra="forbid")

    models: ModelRoles = Field(default_factory=ModelRoles)
    provider_routing: dict[str, ProviderRouting] = Field(default_factory=dict)
    base_url: str | None = None
    api_key_env: str | None = None

    @model_validator(mode="after")
    def _validate_llm(self) -> LLMConfig:
        has_custom = False
        for role in ("translator", "analyst", "editor", "fast"):
            for value in getattr(self.models, role):
                try:
                    provider, model = parse_provider_model(value)
                    validate_model_selection(provider, model)
                except ValueError as error:
                    raise ValueError(f"llm.models.{role}：{error}") from None
                if provider == "openai-compatible":
                    has_custom = True
        for value in self.provider_routing:
            try:
                provider, model = parse_provider_model(value)
            except ValueError as error:
                raise ValueError(f"llm.provider_routing：{error}") from None
            if provider != "openrouter":
                raise ValueError(f"llm.provider_routing 仅支持 openrouter 模型：{provider}/{model}")
        if has_custom:
            if not (self.base_url or "").strip():
                raise ValueError("llm.base_url：openai-compatible 必须配置服务地址")
        elif self.base_url is not None or self.api_key_env is not None:
            raise ValueError(
                "llm.base_url / llm.api_key_env 只用于 openai-compatible；"
                "标准 Provider 使用内置地址和密钥环境变量"
            )
        return self


def _validate_asset_reference(value: str, builtin: str) -> str:
    if not value.strip() or "\0" in value:
        raise ValueError("主题资源路径不能为空或包含 NUL")
    if value.startswith("builtin:") and value != builtin:
        raise ValueError(f"仅支持 {builtin}")
    return value


class BilingualOutputConfig(BaseModel):
    """双语输出开关与原文顺序。"""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = True
    order: Literal["target_first", "source_first"] = "target_first"


class OverrideThemeConfig(BaseModel):
    """通用 EPUB 主题样式。"""

    model_config = ConfigDict(extra="forbid")

    styles: StrictStr

    @field_validator("styles")
    @classmethod
    def _validate_reference(cls, value: str) -> str:
        return _validate_asset_reference(value, "builtin:chinese-reading")


class OutputConfig(BaseModel):
    """单次运行的输出与 EPUB 展示选择。"""

    model_config = ConfigDict(extra="forbid")

    mono: StrictBool = True
    bilingual: BilingualOutputConfig = Field(default_factory=BilingualOutputConfig)
    override_theme: OverrideThemeConfig | None = None
    bilingual_styles: StrictStr = "builtin:bilingual"

    @field_validator("bilingual_styles")
    @classmethod
    def _validate_bilingual_styles(cls, value: str) -> str:
        return _validate_asset_reference(value, "builtin:bilingual")


class _FileConfig(BaseModel):
    """严格的公开 YAML schema。"""

    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    quality: QualityPreset = "balanced"
    output: OutputConfig = Field(default_factory=OutputConfig)


class SegmentConfig(BaseModel):
    """内部切分策略，不属于用户配置。"""

    max_chars_per_batch: int = 1800
    max_chars_per_segment: int = 1200


class PipelineConfig(BaseModel):
    """由质量档位展开的内部流水线策略。"""

    polish: bool
    single_segment_translation: bool = False
    protocol_retry_limit: int = 2
    rolling_context_segments: int = 6
    prescan_concurrency: int = 4
    glossary_scope: Literal["chapter", "full"] = "chapter"
    inflight_glossary: bool = False

    @classmethod
    def for_quality(cls, quality: QualityPreset) -> PipelineConfig:
        common = {
            "protocol_retry_limit": 2,
            "rolling_context_segments": 6,
            "prescan_concurrency": 4,
            "glossary_scope": "chapter",
            "inflight_glossary": False,
        }
        profiles: dict[str, dict[str, Any]] = {
            "economy": {
                "polish": False,
                "single_segment_translation": False,
            },
            "balanced": {
                "polish": False,
                "single_segment_translation": True,
            },
            "quality": {
                "polish": True,
                "single_segment_translation": True,
            },
        }
        return cls.model_validate({**common, **profiles[quality]})


def _resolve_asset_path(value: str, base_dir: str | Path | None) -> str:
    if value.startswith("builtin:"):
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        if base_dir is None:
            raise ValueError("相对主题资源路径必须提供 base_dir")
        path = Path(base_dir).expanduser() / path
    return str(Path(path).absolute())


def _resolve_output_paths(
    output: OutputConfig, base_dir: str | Path | None
) -> tuple[OutputConfig, dict[str, str]]:
    resolved = output.model_copy(deep=True)
    origins: dict[str, str] = {}
    label = str(base_dir) if base_dir is not None else ""
    if resolved.override_theme is not None:
        value = resolved.override_theme.styles
        if not value.startswith("builtin:"):
            resolved.override_theme.styles = _resolve_asset_path(value, base_dir)
            origins["override_theme.styles"] = label
    if not resolved.bilingual_styles.startswith("builtin:"):
        resolved.bilingual_styles = _resolve_asset_path(resolved.bilingual_styles, base_dir)
        origins["bilingual_styles"] = label
    return resolved, origins


def resolve_output(
    current: OutputConfig, saved: OutputConfig | None = None
) -> tuple[OutputConfig, bool]:
    """按各具体字段是否显式设置，依次采用当前、已保存或默认的输出选项。"""

    default = OutputConfig()

    def pick(model: BaseModel, field_name: str, saved_model: BaseModel | None, fallback: Any):
        if field_name in model.model_fields_set:
            return getattr(model, field_name)
        if saved_model is not None and field_name in saved_model.model_fields_set:
            return getattr(saved_model, field_name)
        return fallback

    saved_bilingual = saved.bilingual if saved is not None else None
    selected_theme = pick(current, "override_theme", saved, default.override_theme)
    effective = OutputConfig(
        mono=pick(current, "mono", saved, default.mono),
        bilingual=BilingualOutputConfig(
            enabled=pick(
                current.bilingual,
                "enabled",
                saved_bilingual,
                default.bilingual.enabled,
            ),
            order=pick(
                current.bilingual,
                "order",
                saved_bilingual,
                default.bilingual.order,
            ),
        ),
        override_theme=selected_theme.model_copy(deep=True) if selected_theme is not None else None,
        bilingual_styles=pick(current, "bilingual_styles", saved, default.bilingual_styles),
    )
    normalized = not effective.mono and not effective.bilingual.enabled
    if normalized:
        effective.mono = True
    return effective, normalized


@dataclass
class Config:
    """严格文件配置，加上不暴露到 YAML 的运行时状态。"""

    llm: LLMConfig = field(default_factory=LLMConfig)
    quality: QualityPreset = "balanced"
    source_lang: str = "auto"
    target_lang: str = "zh"
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = field(default_factory=lambda: PipelineConfig.for_quality("balanced"))
    output: OutputConfig = field(default_factory=OutputConfig)
    output_origins: dict[str, str] = field(default_factory=dict)
    honorific_strategy: Literal["keep_style", "normalize", "drop"] = "keep_style"
    punctuation_normalize: bool = True
    state_dir: str = "state"

    @classmethod
    def defaults(cls) -> Config:
        return cls()

    def apply_quality(self, quality: str) -> None:
        parsed = _FileConfig.model_validate({"quality": quality}).quality
        self.quality = parsed
        self.pipeline = PipelineConfig.for_quality(parsed)

    @staticmethod
    def default_config_text() -> str:
        return (
            resources.files("trans_novel")
            .joinpath("config.example.yaml")
            .read_text(encoding="utf-8")
        )

    @classmethod
    def create_default_file(cls, path: str = "config.yaml", *, overwrite: bool = False) -> bool:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        template = cls.default_config_text()
        if overwrite:
            target.write_text(template, encoding="utf-8")
            return True
        try:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(template)
        except FileExistsError:
            return False
        return True

    @classmethod
    def load(cls, path: str = "config.yaml") -> Config:
        target = Path(path).expanduser()
        if not target.is_file():
            return cls.defaults()
        with target.open(encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
        config = cls.from_dict({} if loaded is None else loaded, base_dir=target.parent)
        config.output_origins = {key: str(target) for key in config.output_origins}
        return config

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, base_dir: str | Path | None = None) -> Config:
        if not isinstance(raw, dict):
            raise ValueError("配置文件顶层必须是 YAML 映射")
        deprecated = sorted(_DEPRECATED_ROOT_KEYS.intersection(raw))
        llm_raw = raw.get("llm")
        if isinstance(llm_raw, dict):
            deprecated.extend(
                f"llm.{key}" for key in sorted(_DEPRECATED_LLM_KEYS.intersection(llm_raw))
            )
        if deprecated:
            raise ValueError(
                "配置文件使用了已废弃的格式（"
                + ", ".join(deprecated)
                + "）。请删除 config.yaml 后直接运行，或执行 `wenyi init --force`。"
            )
        output_raw = raw.get("output")
        if isinstance(output_raw, dict) and type(output_raw.get("bilingual")) is bool:
            raise ValueError(
                "output.bilingual 已改为映射；请使用 output.bilingual.enabled: true/false"
            )
        parsed = _FileConfig.model_validate(raw)
        output, output_origins = _resolve_output_paths(parsed.output, base_dir)
        return cls(
            llm=parsed.llm,
            quality=parsed.quality,
            pipeline=PipelineConfig.for_quality(parsed.quality),
            output=output,
            output_origins=output_origins,
        )
