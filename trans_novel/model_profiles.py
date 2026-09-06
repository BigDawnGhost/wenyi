"""模型规格后缀与 Provider/模型能力目录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

RequestDialect = Literal["generic", "deepseek", "bailian", "openai", "openrouter"]
ThinkingLevel = Literal["off", "low", "medium", "high", "max"]
ReasoningEffort = Literal["low", "medium", "high", "max"]

BUILTIN_PROVIDERS: tuple[str, ...] = (
    "deepseek",
    "opencode-go",
    "bailian",
    "openai",
    "openrouter",
    "openai-compatible",
    "ollama",
    "vllm",
    "fake",
)

DIALECT_GENERIC: RequestDialect = "generic"
DIALECT_DEEPSEEK: RequestDialect = "deepseek"
DIALECT_BAILIAN: RequestDialect = "bailian"
DIALECT_OPENAI: RequestDialect = "openai"
DIALECT_OPENROUTER: RequestDialect = "openrouter"
THINKING_LEVELS: tuple[ThinkingLevel, ...] = ("off", "low", "medium", "high", "max")


@dataclass(frozen=True)
class ModelSelection:
    """从 `<model-id>:<thinking-level>` 解析出的模型选择。"""

    model: str
    thinking: ThinkingLevel | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    """某个模型的请求方言与已验证能力；未知能力一律不发送。"""

    request_dialect: RequestDialect = DIALECT_GENERIC
    reasoning_efforts: frozenset[ReasoningEffort] = frozenset()
    catalogued: bool = False
    supports_thinking_disabled: bool = False
    supports_temperature: bool = False
    supports_seed: bool = False
    responses_api: bool = False


_GENERIC_CAPABILITIES = ModelCapabilities()
_DEFAULT_CAPABILITIES: dict[str, ModelCapabilities] = {
    "deepseek": ModelCapabilities(
        request_dialect=DIALECT_DEEPSEEK,
        reasoning_efforts=frozenset({"low", "medium", "high"}),
    ),
    "opencode-go": _GENERIC_CAPABILITIES,
    "bailian": _GENERIC_CAPABILITIES,
    "openai": ModelCapabilities(
        request_dialect=DIALECT_OPENAI,
        reasoning_efforts=frozenset({"low", "medium", "high"}),
    ),
    "openrouter": ModelCapabilities(
        request_dialect=DIALECT_OPENROUTER,
        reasoning_efforts=frozenset({"low", "medium", "high"}),
    ),
    "openai-compatible": _GENERIC_CAPABILITIES,
    "ollama": _GENERIC_CAPABILITIES,
    "vllm": _GENERIC_CAPABILITIES,
    "fake": _GENERIC_CAPABILITIES,
}
_MODEL_CAPABILITIES: dict[tuple[str, str], ModelCapabilities] = {
    # Pi 的发布模型目录将该模型标为 DeepSeek thinking 格式，仅支持 high/max。
    ("opencode-go", "deepseek-v4-flash"): ModelCapabilities(
        request_dialect=DIALECT_DEEPSEEK,
        reasoning_efforts=frozenset({"high", "max"}),
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("opencode-go", "mimo-v2.5"): ModelCapabilities(
        request_dialect=DIALECT_DEEPSEEK,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("opencode-go", "glm-5.3-flash"): ModelCapabilities(
        request_dialect=DIALECT_GENERIC,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("opencode-go", "muse-spark-1.3-contributor"): ModelCapabilities(
        request_dialect=DIALECT_OPENAI,
        reasoning_efforts=frozenset({"low"}),
        catalogued=True,
        supports_temperature=True,
        responses_api=True,
    ),
    ("openrouter", "tencent/hy-mt2-30b-a3b"): ModelCapabilities(
        request_dialect=DIALECT_GENERIC,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("openrouter", "tencent/hy-mt2-30b-a3b-20260521"): ModelCapabilities(
        request_dialect=DIALECT_GENERIC,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("openrouter", "tencent/hy3"): ModelCapabilities(
        request_dialect=DIALECT_OPENROUTER,
        catalogued=True,
        supports_temperature=True,
    ),
    # 百炼的 DeepSeek V4 使用 enable_thinking，并接受四档 reasoning_effort。
    ("bailian", "deepseek-v4-flash"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        reasoning_efforts=frozenset({"low", "medium", "high", "max"}),
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "deepseek-v4-flash-0731"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        reasoning_efforts=frozenset({"low", "medium", "high", "max"}),
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "deepseek-v4-pro"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "deepseek-v4-pro-us"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "deepseek-v4-pro-0813"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    # 千问 3.7 Flash 支持开关思考，但不接受本项目的 reasoning_effort 档位。
    ("bailian", "qwen3.7-flash"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "qwen3.7-flash-2026-07-15"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "qwen3.7-plus"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "qwen3.7-plus-us"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "qwen3.7-plus-2026-05-26"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
    ("bailian", "qwen3.8-max"): ModelCapabilities(
        request_dialect=DIALECT_BAILIAN,
        catalogued=True,
        supports_thinking_disabled=True,
        supports_temperature=True,
    ),
}


def parse_provider_model(value: str) -> tuple[str, str]:
    """Parse public ``provider/model`` syntax, splitting only its first slash."""
    if not isinstance(value, str):
        raise ValueError("model selection must be a string")
    provider, separator, model = value.partition("/")
    if not separator:
        raise ValueError("model selection must use provider/model syntax")
    provider = provider.strip()
    model = model.strip()
    if provider not in BUILTIN_PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    if not model:
        raise ValueError("model ID must not be empty")
    return provider, model


def parse_model_selection(value: str) -> ModelSelection:
    """仅把最右侧的已知级别识别为后缀；`qwen:32b` 等模型 ID 保持不变。"""

    model, separator, suffix = value.rpartition(":")
    if separator and model and suffix in THINKING_LEVELS:
        return ModelSelection(model=model, thinking=cast(ThinkingLevel, suffix))
    return ModelSelection(model=value)


def capabilities_for(provider: str, model: str) -> ModelCapabilities:
    return _MODEL_CAPABILITIES.get(
        (provider, model),
        _DEFAULT_CAPABILITIES.get(provider, _GENERIC_CAPABILITIES),
    )


def validate_model_selection(provider: str, value: str) -> ModelSelection:
    """校验显式 thinking 后缀；不支持时列出该模型可用级别。"""

    selection = parse_model_selection(value)
    if selection.thinking is None:
        model, separator, suffix = value.rpartition(":")
        if separator and (provider, model) in _MODEL_CAPABILITIES:
            allowed = ", ".join(THINKING_LEVELS)
            raise ValueError(f"未知 thinking 级别 {suffix!r}；可选：{allowed}")
        return selection

    capabilities = capabilities_for(provider, selection.model)
    if selection.thinking != "off" and selection.thinking not in capabilities.reasoning_efforts:
        supported = [
            "off",
            *(level for level in THINKING_LEVELS if level in capabilities.reasoning_efforts),
        ]
        raise ValueError(
            f"{provider}:{selection.model} 不支持 thinking 级别 {selection.thinking!r}；"
            f"支持：{', '.join(supported)}"
        )
    return selection
