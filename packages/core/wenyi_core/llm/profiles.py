"""Declarative provider profiles: one record per inference provider Wenyi supports.

A profile is pure data plus, at most, one small pure function describing how a provider
wants reasoning controls placed on the wire. Everything else -- credentials, transport,
retries, usage accounting -- lives in the wire adapters and the shared routing layer.

Adding a provider is one entry here; ``registry.py`` turns every profile into a routable
provider without a per-vendor module.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import reasoning as effort

# Wire protocols. Each name maps to one adapter module in ``wenyi_core.llm.providers``.
WIRE_OPENAI_CHAT = "openai_chat"
WIRE_ANTHROPIC = "anthropic_messages"
WIRE_RESPONSES = "openai_responses"
WIRE_GEMINI_NATIVE = "gemini_native"
WIRE_GEMINI_CLOUDCODE = "gemini_cloudcode"

# Authentication mechanisms.
AUTH_API_KEY = "api_key"
AUTH_OAUTH_DEVICE = "oauth_device"
AUTH_OAUTH_USER_CODE = "oauth_user_code"
AUTH_OAUTH_PKCE = "oauth_pkce"
AUTH_OAUTH_IMPORT = "oauth_import"
AUTH_COPILOT = "copilot"
AUTH_VERTEX = "vertex"


@dataclass(frozen=True)
class ReasoningRequest:
    """One normalized reasoning preference plus the model it targets."""

    model: str
    enabled: bool
    effort: str | None


ReasoningBuilder = Callable[[ReasoningRequest], "tuple[dict[str, Any], dict[str, Any]]"]


@dataclass(frozen=True)
class ProviderProfile:
    """Everything Wenyi needs to route to one provider without vendor code."""

    kind: str
    wire: str
    base_url: str | None = None
    aliases: tuple[str, ...] = ()
    display_name: str = ""
    description: str = ""
    signup_url: str = ""
    # Ordered candidate environment variables for the credential; the first non-empty wins.
    api_key_envs: tuple[str, ...] = ()
    base_url_env: str | None = None
    models_url: str | None = None
    auth: str = AUTH_API_KEY
    # ``authorization`` (Bearer) or ``x-api-key``.
    api_key_header: str = "authorization"
    default_headers: Mapping[str, str] = field(default_factory=dict)
    auth_headers: Mapping[str, str] = field(default_factory=dict)
    requires_base_url: bool = True
    requires_api_key: bool = True
    supports_model_listing: bool = True
    supports_vision: bool = False
    supports_prompt_cache_key: bool = False
    unsupported_response_formats: tuple[str, ...] = ()
    # Static fields every request must carry for this provider, merged before reasoning.
    default_body: Mapping[str, Any] = field(default_factory=dict)
    default_max_tokens: int | None = None
    # Reasoning is on unless a model profile turns it off; providers without a reasoning
    # builder never see the toggle.
    default_thinking: bool = True
    default_effort: str | None = None
    # Tier -> model ID for ``llm.preset: <kind>``.
    presets: Mapping[str, str] = field(default_factory=dict)
    fallback_models: tuple[str, ...] = ()
    reasoning: ReasoningBuilder | None = None
    # Key into ``wenyi_core.llm.subscriptions.SUBSCRIPTIONS`` for interactive login.
    subscription: str | None = None
    notes: str = ""

    def display(self) -> str:
        """Return the human-facing provider name."""
        return self.display_name or self.kind

    def preset_models(self) -> dict[str, str]:
        """Resolve the strong/cheap/fast tier defaults for this profile."""
        tiers = ("strong", "cheap", "fast")
        if self.presets:
            filled = {**self.presets}
            for tier in tiers:
                if tier not in filled:
                    filled[tier] = filled["strong"]
            return {tier: filled[tier] for tier in tiers}
        if not self.fallback_models:
            return {}
        picks = [self.fallback_models[0]]
        picks.append(self.fallback_models[1] if len(self.fallback_models) > 1 else picks[0])
        picks.append(self.fallback_models[2] if len(self.fallback_models) > 2 else picks[1])
        return dict(zip(tiers, picks))


# ── Reasoning builders ────────────────────────────────────────────────────────────────────
# Each builder returns ``(extra_body, top_level)``; the chat/responses adapters merge
# ``extra_body`` into the request body and ``top_level`` into the top-level SDK arguments.


def _top_level_effort(
    supported: tuple[str, ...],
    *,
    overrides: Mapping[str, str] | None = None,
    default: str | None = None,
    disabled: str | None = "none",
    key: str = "reasoning_effort",
) -> ReasoningBuilder:
    """Emit only a top-level effort field, clamped to the provider's vocabulary."""

    def build(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        if not request.enabled:
            return ({}, {key: disabled} if disabled is not None else {})
        requested = effort.effective_effort(
            enabled=True, effort=request.effort, default_effort=default
        )
        if requested is None or requested == "none":
            return {}, {}
        clamped = effort.clamp_effort(requested, supported, overrides)
        if clamped is None or clamped == "none":
            return {}, {}
        return {}, {key: clamped}

    return build


def _thinking_toggle(
    *,
    supported: tuple[str, ...] | None = None,
    overrides: Mapping[str, str] | None = None,
    default: str | None = None,
    always_emit_toggle: bool = False,
    toggle_key: str = "thinking",
    effort_key: str | None = "reasoning_effort",
) -> ReasoningBuilder:
    """Emit an ``extra_body`` thinking toggle plus an optional top-level effort."""

    def build(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}
        if not request.enabled:
            extra_body[toggle_key] = {"type": "disabled"}
            return extra_body, top_level
        requested = effort.effective_effort(
            enabled=True, effort=request.effort, default_effort=default
        )
        if always_emit_toggle and requested != "none":
            extra_body[toggle_key] = {"type": "enabled"}
        if requested is None or requested == "none":
            return extra_body, top_level
        clamped = effort.clamp_effort(requested, supported, overrides)
        if clamped and clamped != "none" and effort_key is not None:
            top_level[effort_key] = clamped
        return extra_body, top_level

    return build


def _openrouter_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """OpenRouter carries reasoning as ``extra_body.reasoning``."""
    if not request.enabled:
        return {"reasoning": {"enabled": False}}, {}
    requested = request.effort or "medium"
    clamped = effort.clamp_effort(requested, effort.OPENAI_COMPAT_WIRE_EFFORTS)
    return {"reasoning": {"enabled": True, "effort": clamped or "medium"}}, {}


def _vercel_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Vercel AI Gateway mirrors the OpenRouter ``reasoning`` object."""
    if not request.enabled:
        return {"reasoning": {"enabled": False}}, {}
    requested = request.effort or "medium"
    clamped = effort.clamp_effort(requested, effort.OPENAI_COMPAT_WIRE_EFFORTS)
    return {"reasoning": {"enabled": True, "effort": clamped or "medium"}}, {}


def _glm_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Z.AI GLM: ``extra_body.thinking`` toggle, plus GLM-5.2/5.3 native effort."""
    model = request.model.strip().lower()
    is_53 = any(token in model for token in ("glm-5.3", "glm-5-3", "glm-5p3"))
    is_52 = is_53 or any(token in model for token in ("glm-5.2", "glm-5-2", "glm-5p2"))
    extra_body: dict[str, Any] = {}
    top_level: dict[str, Any] = {}
    if not is_52 and not _glm_supports_thinking(model):
        return extra_body, top_level
    if not request.enabled:
        extra_body["thinking"] = {"type": "disabled"}
        return extra_body, top_level
    if request.effort:
        supported = effort.GLM_53_EFFORTS if is_53 else effort.GLM_52_EFFORTS
        floor = "low" if is_53 else "high"
        clamped = effort.clamp_effort(request.effort, supported, effort.GLM_OR_DEEPSEEK_OVERRIDES)
        top_level["reasoning_effort"] = clamped if clamped in supported else floor
    if is_52:
        extra_body["thinking"] = {"type": "enabled"}
    return extra_body, top_level


def _glm_supports_thinking(model: str) -> bool:
    """GLM 4.5 and later expose a thinking toggle."""
    import re

    match = re.match(r"^glm-(\d+)(?:\.(\d+))?", model)
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2) or 0)) >= (4, 5)


def _deepseek_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """DeepSeek V4+ takes an explicit toggle plus top-level effort."""
    model = _flat_model(request.model)
    thinking_capable = (
        model.startswith("deepseek-v") and not model.startswith("deepseek-v3")
    ) or model in {"deepseek-flash", "deepseek-reasoner"}
    if not thinking_capable:
        return {}, {}
    extra_body = {"thinking": {"type": "enabled" if request.enabled else "disabled"}}
    if not request.enabled:
        return extra_body, {}
    requested = request.effort or "medium"
    clamped = effort.clamp_effort(
        requested, effort.DEEPSEEK_V4_EFFORTS, effort.GLM_OR_DEEPSEEK_OVERRIDES
    )
    return extra_body, ({"reasoning_effort": clamped} if clamped else {})


def _kimi_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Moonshot treats the toggle and the effort field as mutually exclusive."""
    model = _flat_model(request.model)
    if _is_k3(model):
        supported, overrides = effort.KIMI_K3_EFFORTS, effort.KIMI_K3_OVERRIDES
    else:
        supported, overrides = effort.KIMI_EFFORTS, None
    if not request.enabled:
        return {"thinking": {"type": "disabled"}}, {}
    if request.effort:
        clamped = effort.clamp_effort(request.effort, supported, overrides)
        return {}, ({"reasoning_effort": clamped} if clamped else {})
    return {}, {}


def _is_k3(model: str) -> bool:
    """Match the K3 slug (``k3``, ``k3-256k``, ``kimi-k3*``) but never K2-era names."""
    import re

    return bool(re.search(r"(?:^|[^a-z0-9])k3(?:[^a-z0-9]|$)", model))


def _ollama_cloud_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ollama Cloud uses the top-level effort as its only working off switch."""
    requested = effort.effective_effort(
        enabled=request.enabled, effort=request.effort, default_effort="medium"
    )
    if requested is None:
        return {}, {}
    clamped = effort.clamp_effort(
        requested, effort.OLLAMA_CLOUD_EFFORTS, effort.GLM_OR_DEEPSEEK_OVERRIDES
    )
    return {}, ({"reasoning_effort": clamped} if clamped else {})


def _solar_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Upstage Solar defaults to reasoning on at medium for agentic work."""
    model = _flat_model(request.model)
    if model.startswith(("solar-mini", "syn-pro")):
        return {}, {}
    requested = effort.effective_effort(
        enabled=request.enabled, effort=request.effort, default_effort="medium"
    )
    if requested is None:
        return {}, {}
    if requested == "minimal":
        return {}, {}
    clamped = effort.clamp_effort(requested, effort.SOLAR_EFFORTS)
    if clamped is None:
        upgraded = "high" if requested not in effort.EFFORT_LADDER else None
        return {}, ({"reasoning_effort": upgraded} if upgraded else {})
    return {}, {"reasoning_effort": clamped}


def _meta_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Meta Muse rejects ``none``; disabled collapses to ``minimal``."""
    if not request.enabled or (request.effort or "") == "none":
        return {}, {"reasoning_effort": "minimal"}
    clamped = effort.clamp_effort(request.effort, effort.META_AI_EFFORTS)
    return {}, {"reasoning_effort": clamped or "medium"}


def _nebius_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Nebius accepts plain low/medium/high with an unset default of medium."""
    if not request.enabled:
        return {}, {}
    requested = request.effort or "medium"
    clamped = effort.clamp_effort(requested, effort.SOLAR_EFFORTS)
    return {}, {"reasoning_effort": clamped or "medium"}


def _minimax_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """MiniMax M3 on the OpenAI-compatible route keeps thinking inline."""
    model = _flat_model(request.model)
    if model not in {"minimax-m3", "minimax/minimax-m3"}:
        return {}, {}
    extra_body: dict[str, Any] = {"reasoning_split": True}
    extra_body["thinking"] = {"type": "adaptive" if request.enabled else "disabled"}
    return extra_body, {}


def _fireworks_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fireworks rejects nested reasoning objects; the top-level effort is the control."""
    if not request.enabled:
        return {}, {"reasoning_effort": "none"}
    return {}, ({"reasoning_effort": request.effort} if request.effort else {})


def _xai_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """xAI Responses: Grok 4.6+ accepts xhigh, older Grok stops at high."""
    model = _flat_model(request.model)
    supported = (
        effort.XAI_EFFORTS if "4.6" in model or "4-6" in model else effort.XAI_LEGACY_EFFORTS
    )
    if not request.enabled:
        return {}, {}
    requested = request.effort or "medium"
    clamped = effort.clamp_effort(requested, supported)
    return {}, {"reasoning_effort": clamped or "medium"}


def _router_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ramp Router accepts the widest OpenAI-compatible vocabulary."""
    return _top_level_effort(effort.OPENAI_COMPAT_WIRE_EFFORTS)(request)


def _codex_reasoning(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Codex Responses carries one ``reasoning`` object with the effort and a summary mode."""
    if not request.enabled:
        return {}, {}
    requested = effort.effective_effort(enabled=True, effort=request.effort, default_effort="high")
    # ``max`` is advertised by the newest generation only, so clamp to the wider-safe set.
    clamped = effort.clamp_effort(requested, effort.CODEX_LEGACY_EFFORTS)
    if clamped is None or clamped == "none":
        return {}, {}
    return {}, {"reasoning": {"effort": clamped, "summary": "auto"}}


def _reasoning_object(request: ReasoningRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    """Nous Portal passes the full ``reasoning`` object through."""
    if not request.enabled:
        return {}, {}
    return {"reasoning": {"enabled": True, "effort": request.effort or "medium"}}, {}


def _flat_model(model: str) -> str:
    """Bare model ID, tolerating aggregator prefixes."""
    return (model or "").strip().rsplit("/", 1)[-1].lower()


# ── Profile table ─────────────────────────────────────────────────────────────────────────

_APPLICATION_JSON = {"accept": "application/json"}


def _antigravity_headers() -> dict[str, str]:
    """Present the desktop client identity the Antigravity Cloud Code endpoint expects."""
    os_type = platform.system().lower() or "unknown"
    arch = platform.machine() or "unknown"
    return {
        "user-agent": "antigravity/hub/2.12.2 "
        f"(aidev_client; os_type={os_type}; arch={arch}; cl=975423596)"
    }


def _profile(
    kind: str,
    wire: str,
    base_url: str | None,
    api_key_envs: tuple[str, ...] = (),
    **kwargs: Any,
) -> ProviderProfile:
    return ProviderProfile(
        kind=kind,
        wire=wire,
        base_url=base_url,
        api_key_envs=api_key_envs,
        **kwargs,
    )


PROFILES: tuple[ProviderProfile, ...] = (
    # ── First-party APIs ─────────────────────────────────────────────────────────────
    _profile(
        "deepseek",
        WIRE_OPENAI_CHAT,
        "https://api.deepseek.com/v1",
        ("DEEPSEEK_API_KEY",),
        display_name="DeepSeek",
        description="DeepSeek native API",
        signup_url="https://platform.deepseek.com/",
        aliases=("deepseek-chat", "deep-seek"),
        unsupported_response_formats=("json_schema",),
        default_effort="high",
        presets={"strong": "deepseek-v4-pro", "cheap": "deepseek-flash", "fast": "deepseek-flash"},
        fallback_models=("deepseek-v4-pro", "deepseek-flash"),
        reasoning=_deepseek_reasoning,
    ),
    _profile(
        "openai",
        WIRE_OPENAI_CHAT,
        "https://api.openai.com/v1",
        ("OPENAI_API_KEY",),
        display_name="OpenAI",
        description="OpenAI Chat Completions",
        signup_url="https://platform.openai.com/api-keys",
        aliases=("openai-api",),
        default_effort="high",
        presets={"strong": "gpt-5.4", "cheap": "gpt-5.4-mini", "fast": "gpt-5.4-mini"},
        fallback_models=("gpt-5.4", "gpt-5.4-mini"),
        reasoning=_top_level_effort(effort.OPENAI_COMPAT_WIRE_EFFORTS),
    ),
    _profile(
        "anthropic",
        WIRE_ANTHROPIC,
        "https://api.anthropic.com",
        ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        display_name="Anthropic",
        description="Anthropic Messages API",
        signup_url="https://console.anthropic.com/settings/keys",
        aliases=("claude", "claude-code"),
        api_key_header="x-api-key",
        auth_headers={"anthropic-version": "2023-06-01"},
        supports_vision=True,
        presets={
            "strong": "claude-sonnet-4-6",
            "cheap": "claude-haiku-4-5-20251001",
            "fast": "claude-haiku-4-5-20251001",
        },
        fallback_models=("claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
    ),
    _profile(
        "gemini",
        WIRE_GEMINI_NATIVE,
        "https://generativelanguage.googleapis.com",
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        display_name="Google Gemini",
        description="Google AI Studio via the google-genai SDK",
        signup_url="https://aistudio.google.com/app/apikey",
        aliases=("google", "google-ai-studio"),
        supports_vision=True,
        presets={
            "strong": "gemini-3.6-flash",
            "cheap": "gemini-3.6-flash",
            "fast": "gemini-3.6-flash",
        },
        fallback_models=("gemini-3.6-flash",),
    ),
    _profile(
        "xai",
        WIRE_RESPONSES,
        "https://api.x.ai/v1",
        ("XAI_API_KEY", "WENYI_XAI_OAUTH"),
        display_name="xAI (Grok)",
        description="xAI Grok via the Responses API (API key or Grok OAuth)",
        signup_url="https://console.x.ai/",
        aliases=("grok", "x-ai"),
        auth=AUTH_OAUTH_DEVICE,
        supports_vision=True,
        default_effort="medium",
        presets={"strong": "grok-4.6", "cheap": "grok-4.1-fast", "fast": "grok-4.1-fast"},
        fallback_models=("grok-4.6", "grok-4.1-fast"),
        reasoning=_xai_reasoning,
        subscription="xai",
    ),
    # ── Aggregators and gateways ─────────────────────────────────────────────────────
    _profile(
        "openrouter",
        WIRE_OPENAI_CHAT,
        "https://openrouter.ai/api/v1",
        ("OPENROUTER_API_KEY",),
        display_name="OpenRouter",
        description="OpenRouter unified API for 200+ models",
        signup_url="https://openrouter.ai/keys",
        aliases=("or",),
        models_url="https://openrouter.ai/api/v1/models",
        supports_vision=True,
        supports_prompt_cache_key=True,
        default_effort="medium",
        presets={
            "strong": "anthropic/claude-sonnet-4.6",
            "cheap": "deepseek/deepseek-chat",
            "fast": "google/gemini-3.6-flash",
        },
        fallback_models=(
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4",
            "google/gemini-3.6-flash",
        ),
        reasoning=_openrouter_reasoning,
    ),
    _profile(
        "router",
        WIRE_RESPONSES,
        "https://api.router.com/v1",
        ("RAMP_ROUTER_API_KEY", "ROUTER_API_KEY"),
        base_url_env="RAMP_ROUTER_BASE_URL",
        display_name="Ramp Router",
        description="Ramp Router routes each request to the cheapest model that clears your bar",
        signup_url="https://app.router.com/keys",
        aliases=("ramp-router", "ramp"),
        supports_vision=True,
        default_effort="medium",
        fallback_models=(),
        reasoning=_router_reasoning,
    ),
    _profile(
        "ai-gateway",
        WIRE_OPENAI_CHAT,
        "https://ai-gateway.vercel.sh/v1",
        ("AI_GATEWAY_API_KEY",),
        display_name="Vercel AI Gateway",
        description="Vercel AI Gateway with attribution headers",
        signup_url="https://vercel.com/docs/ai-gateway",
        aliases=("vercel", "vercel-ai-gateway"),
        default_headers={"HTTP-Referer": "https://wenyi.app", "X-Title": "Wenyi"},
        default_effort="medium",
        presets={"strong": "openai/gpt-5.4", "cheap": "google/gemini-3-flash"},
        fallback_models=("openai/gpt-5.4", "google/gemini-3-flash"),
        reasoning=_vercel_reasoning,
    ),
    _profile(
        "huggingface",
        WIRE_OPENAI_CHAT,
        "https://router.huggingface.co/v1",
        ("HF_TOKEN",),
        display_name="HuggingFace",
        description="HuggingFace Inference API",
        signup_url="https://huggingface.co/settings/tokens",
        aliases=("hf", "hugging-face"),
        presets={"strong": "Qwen/Qwen3.5-72B-Instruct", "cheap": "deepseek-ai/DeepSeek-V3.2"},
        fallback_models=("Qwen/Qwen3.5-72B-Instruct", "deepseek-ai/DeepSeek-V3.2"),
    ),
    _profile(
        "nous",
        WIRE_OPENAI_CHAT,
        "https://inference-api.nousresearch.com/v1",
        ("NOUS_API_KEY", "WENYI_NOUS_OAUTH"),
        display_name="Nous Research",
        description="Nous Research Hermes model family (device-code sign-in supported)",
        signup_url="https://nousresearch.com/",
        aliases=("nous-portal", "nousresearch"),
        auth=AUTH_OAUTH_DEVICE,
        default_effort="medium",
        presets={"strong": "hermes-4-405b", "cheap": "hermes-4-70b"},
        fallback_models=("hermes-4-405b", "hermes-4-70b"),
        reasoning=_reasoning_object,
        subscription="nous",
    ),
    _profile(
        "qwen-oauth",
        WIRE_OPENAI_CHAT,
        "https://portal.qwen.ai/v1",
        ("QWEN_API_KEY", "WENYI_QWEN_OAUTH"),
        display_name="Qwen Portal",
        description="Qwen Portal subscription (Qwen CLI credentials can be imported)",
        signup_url="https://chat.qwen.ai/",
        aliases=("qwen", "qwen-portal"),
        auth=AUTH_OAUTH_IMPORT,
        default_max_tokens=65536,
        supports_vision=True,
        presets={"strong": "qwen3-max", "cheap": "qwen3-coder-flash"},
        fallback_models=("qwen3-max", "qwen3-coder-flash"),
        subscription="qwen",
    ),
    _profile(
        "opencode-zen",
        WIRE_OPENAI_CHAT,
        "https://opencode.ai/zen/v1",
        ("OPENCODE_ZEN_API_KEY",),
        display_name="OpenCode Zen",
        description="OpenCode Zen model gateway",
        aliases=("opencode", "zen"),
        default_headers={"HTTP-Referer": "https://wenyi.app", "X-Title": "Wenyi"},
        presets={"strong": "claude-sonnet-4-6", "cheap": "gemini-3-flash"},
        fallback_models=("claude-sonnet-4-6", "gemini-3-flash"),
    ),
    _profile(
        "opencode-go",
        WIRE_OPENAI_CHAT,
        "https://opencode.ai/zen/go/v1",
        ("OPENCODE_API_KEY", "OPENCODE_GO_API_KEY"),
        display_name="OpenCode Go",
        description="OpenCode Go subscription relay",
        aliases=("opencode-go-sub",),
        supports_vision=True,
        presets={"strong": "glm-5", "cheap": "glm-5"},
        fallback_models=("glm-5",),
        reasoning=_top_level_effort(effort.OPENAI_COMPAT_WIRE_EFFORTS),
    ),
    _profile(
        "kilocode",
        WIRE_OPENAI_CHAT,
        "https://api.kilo.ai/api/gateway",
        ("KILOCODE_API_KEY",),
        display_name="Kilo Code",
        description="Kilo Code gateway",
        aliases=("kilo", "kilo-code"),
        presets={"strong": "google/gemini-3.6-flash", "cheap": "google/gemini-3.6-flash"},
        fallback_models=("google/gemini-3.6-flash",),
    ),
    _profile(
        "commandcode",
        WIRE_OPENAI_CHAT,
        "https://api.commandcode.ai/provider/v1",
        ("COMMANDCODE_API_KEY",),
        base_url_env="COMMANDCODE_BASE_URL",
        display_name="CommandCode",
        description="CommandCode OpenAI-compatible endpoint",
        signup_url="https://commandcode.ai/",
        models_url="https://api.commandcode.ai/provider/v1/models",
        presets={"strong": "deepseek/deepseek-v4-pro", "cheap": "deepseek/deepseek-v4-flash"},
        fallback_models=("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"),
    ),
    _profile(
        "commandcode-anthropic",
        WIRE_ANTHROPIC,
        "https://api.commandcode.ai/provider/v1",
        ("COMMANDCODE_API_KEY",),
        base_url_env="COMMANDCODE_ANTHROPIC_BASE_URL",
        display_name="CommandCode (Anthropic)",
        description="Claude models through CommandCode's Messages endpoint",
        signup_url="https://commandcode.ai/",
        aliases=("commandcode-claude",),
        presets={
            "strong": "claude-sonnet-4-6",
            "cheap": "claude-haiku-4-5-20251001",
        },
        fallback_models=("claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
    ),
    # ── Chinese and regional clouds ──────────────────────────────────────────────────
    _profile(
        "alibaba",
        WIRE_OPENAI_CHAT,
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ("DASHSCOPE_API_KEY",),
        base_url_env="DASHSCOPE_BASE_URL",
        display_name="Alibaba Cloud DashScope",
        description="DashScope international OpenAI-compatible endpoint",
        aliases=("dashscope", "qwen-dashscope"),
        presets={"strong": "qwen3-max", "cheap": "qwen3-coder-flash"},
        fallback_models=("qwen3-max", "qwen3-coder-flash"),
    ),
    _profile(
        "alibaba-cn",
        WIRE_OPENAI_CHAT,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ("DASHSCOPE_API_KEY",),
        base_url_env="DASHSCOPE_CN_BASE_URL",
        display_name="Alibaba Cloud DashScope (China)",
        description="DashScope mainland-China endpoint",
        aliases=("dashscope-cn",),
        presets={"strong": "qwen3-max", "cheap": "qwen3-coder-flash"},
        fallback_models=("qwen3-max", "qwen3-coder-flash"),
    ),
    _profile(
        "alibaba-token-plan",
        WIRE_OPENAI_CHAT,
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        ("ALIBABA_TOKEN_PLAN_API_KEY",),
        base_url_env="ALIBABA_TOKEN_PLAN_BASE_URL",
        display_name="Alibaba Cloud (Token Plan)",
        description="Model Studio flat-token tier",
        signup_url="https://help.aliyun.com/zh/model-studio/",
        aliases=("dashscope-token-plan",),
        presets={"strong": "qwen3-max", "cheap": "qwen3-coder-flash"},
        fallback_models=("qwen3-max", "qwen3-coder-flash"),
    ),
    _profile(
        "alibaba-token-plan-cn",
        WIRE_OPENAI_CHAT,
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ("ALIBABA_TOKEN_PLAN_CN_API_KEY", "ALIBABA_TOKEN_PLAN_API_KEY"),
        base_url_env="ALIBABA_TOKEN_PLAN_CN_BASE_URL",
        display_name="Alibaba Cloud (Token Plan, China)",
        description="Model Studio flat-token tier, mainland-China endpoint",
        aliases=("dashscope-token-plan-cn",),
        presets={"strong": "qwen3-max", "cheap": "qwen3-coder-flash"},
        fallback_models=("qwen3-max", "qwen3-coder-flash"),
    ),
    _profile(
        "alibaba-coding-plan",
        WIRE_OPENAI_CHAT,
        "https://coding-intl.dashscope.aliyuncs.com/v1",
        ("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_env="ALIBABA_CODING_PLAN_BASE_URL",
        display_name="Alibaba Cloud (Coding Plan)",
        description="Dedicated coding tier",
        signup_url="https://help.aliyun.com/zh/model-studio/",
        aliases=("dashscope-coding",),
        presets={"strong": "qwen3-max", "cheap": "qwen3-coder-flash"},
        fallback_models=("qwen3-max", "qwen3-coder-flash"),
    ),
    _profile(
        "alibaba-coding-plan-cn",
        WIRE_OPENAI_CHAT,
        "https://coding.dashscope.aliyuncs.com/v1",
        (
            "ALIBABA_CODING_PLAN_CN_API_KEY",
            "ALIBABA_CODING_PLAN_API_KEY",
            "DASHSCOPE_API_KEY",
        ),
        base_url_env="ALIBABA_CODING_PLAN_CN_BASE_URL",
        display_name="Alibaba Cloud (Coding Plan, China)",
        description="Coding tier, mainland-China endpoint",
        aliases=("dashscope-coding-cn",),
        presets={"strong": "qwen3-max", "cheap": "qwen3-coder-flash"},
        fallback_models=("qwen3-max", "qwen3-coder-flash"),
    ),
    _profile(
        "zai",
        WIRE_OPENAI_CHAT,
        "https://api.z.ai/api/paas/v4",
        ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        display_name="Z.AI (GLM)",
        description="Z.AI / Zhipu GLM models",
        signup_url="https://z.ai/",
        aliases=("glm", "z-ai", "zhipu"),
        base_url_env="GLM_BASE_URL",
        presets={"strong": "glm-5.2", "cheap": "glm-4.5-flash"},
        fallback_models=("glm-5.2", "glm-4.5-flash"),
        reasoning=_glm_reasoning,
    ),
    _profile(
        "zai-cn",
        WIRE_OPENAI_CHAT,
        "https://open.bigmodel.cn/api/paas/v4",
        ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        display_name="Z.AI (China)",
        aliases=("zhipu-cn",),
        base_url_env="GLM_BASE_URL",
        presets={"strong": "glm-5.2", "cheap": "glm-4.5-flash"},
        reasoning=_glm_reasoning,
    ),
    _profile(
        "zai-coding-plan",
        WIRE_OPENAI_CHAT,
        "https://api.z.ai/api/coding/paas/v4",
        ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        display_name="Z.AI (Coding Plan)",
        aliases=("zai-coding",),
        base_url_env="GLM_BASE_URL",
        presets={"strong": "glm-5.2", "cheap": "glm-5.2"},
        reasoning=_glm_reasoning,
    ),
    _profile(
        "zai-coding-plan-cn",
        WIRE_OPENAI_CHAT,
        "https://open.bigmodel.cn/api/coding/paas/v4",
        ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        display_name="Z.AI (Coding Plan, China)",
        aliases=("zai-coding-cn",),
        base_url_env="GLM_BASE_URL",
        presets={"strong": "glm-5.2", "cheap": "glm-5.2"},
        reasoning=_glm_reasoning,
    ),
    _profile(
        "kimi-coding",
        WIRE_OPENAI_CHAT,
        "https://api.moonshot.ai/v1",
        ("KIMI_API_KEY", "KIMI_CODING_API_KEY"),
        display_name="Kimi (Moonshot)",
        description="Moonshot Kimi coding tier",
        aliases=("kimi", "moonshot", "kimi-for-coding"),
        base_url_env="KIMI_BASE_URL",
        default_headers={**_APPLICATION_JSON, "accept-encoding": "gzip"},
        default_max_tokens=32000,
        presets={"strong": "kimi-k2.6", "cheap": "kimi-k2-turbo-preview"},
        fallback_models=("kimi-k2.6", "kimi-k2-turbo-preview"),
        reasoning=_kimi_reasoning,
        notes="Kimi Code keys use Messages; legacy Moonshot keys use Chat Completions.",
    ),
    _profile(
        "kimi-coding-cn",
        WIRE_OPENAI_CHAT,
        "https://api.moonshot.cn/v1",
        ("KIMI_CN_API_KEY",),
        display_name="Kimi (Moonshot, China)",
        description="Moonshot mainland-China endpoint",
        aliases=("kimi-cn", "moonshot-cn"),
        base_url_env="KIMI_CN_BASE_URL",
        default_headers={**_APPLICATION_JSON, "accept-encoding": "gzip"},
        default_max_tokens=32000,
        presets={"strong": "kimi-k2.6", "cheap": "kimi-k2-turbo-preview"},
        fallback_models=("kimi-k2.6", "kimi-k2-turbo-preview"),
        reasoning=_kimi_reasoning,
        notes="Kimi Code keys use Messages; legacy Moonshot keys use Chat Completions.",
    ),
    _profile(
        "minimax",
        WIRE_ANTHROPIC,
        "https://api.minimax.io/anthropic",
        ("MINIMAX_API_KEY",),
        base_url_env="MINIMAX_BASE_URL",
        display_name="MiniMax",
        description="MiniMax Messages-compatible endpoint",
        aliases=("mini-max",),
        presets={"strong": "MiniMax-M3", "cheap": "MiniMax-M3"},
        fallback_models=("MiniMax-M3",),
    ),
    _profile(
        "minimax-cn",
        WIRE_ANTHROPIC,
        "https://api.minimaxi.com/anthropic",
        ("MINIMAX_CN_API_KEY",),
        base_url_env="MINIMAX_CN_BASE_URL",
        display_name="MiniMax (China)",
        description="MiniMax mainland-China endpoint",
        aliases=("minimax-china",),
        presets={"strong": "MiniMax-M3", "cheap": "MiniMax-M3"},
        fallback_models=("MiniMax-M3",),
    ),
    _profile(
        "minimax-oauth",
        WIRE_ANTHROPIC,
        "https://api.minimax.io/anthropic",
        ("WENYI_MINIMAX_OAUTH",),
        display_name="MiniMax (OAuth)",
        description="MiniMax via user-code sign-in, no API key required",
        signup_url="https://api.minimax.io/",
        aliases=("minimax-portal",),
        auth=AUTH_OAUTH_USER_CODE,
        presets={"strong": "MiniMax-M2.7", "cheap": "MiniMax-M2.7"},
        fallback_models=("MiniMax-M2.7",),
        subscription="minimax",
    ),
    _profile(
        "stepfun",
        WIRE_OPENAI_CHAT,
        "https://api.stepfun.ai/step_plan/v1",
        ("STEPFUN_API_KEY",),
        base_url_env="STEPFUN_BASE_URL",
        display_name="StepFun Step Plan",
        description="StepFun step_plan tier",
        aliases=("step", "stepfun-coding-plan"),
        presets={"strong": "step-3.5", "cheap": "step-3.5-flash"},
        fallback_models=("step-3.5", "step-3.5-flash"),
    ),
    _profile(
        "xiaomi",
        WIRE_OPENAI_CHAT,
        "https://api.xiaomimimo.com/v1",
        ("XIAOMI_API_KEY",),
        base_url_env="XIAOMI_BASE_URL",
        display_name="Xiaomi MiMo",
        description="Xiaomi MiMo OpenAI-compatible endpoint",
        aliases=("mimo", "xiaomi-mimo"),
        supports_vision=True,
        presets={"strong": "mimo-v2.5-pro", "cheap": "mimo-v2.5-flash"},
        fallback_models=("mimo-v2.5-pro", "mimo-v2.5-flash"),
    ),
    _profile(
        "upstage",
        WIRE_OPENAI_CHAT,
        "https://api.upstage.ai/v1",
        ("UPSTAGE_API_KEY",),
        base_url_env="UPSTAGE_BASE_URL",
        display_name="Upstage Solar",
        description="Upstage Solar API",
        signup_url="https://console.upstage.ai/api-keys",
        aliases=("solar",),
        default_effort="medium",
        presets={"strong": "solar-pro3", "cheap": "solar-mini"},
        fallback_models=("solar-pro3", "solar-mini"),
        reasoning=_solar_reasoning,
    ),
    # ── Third-party inference clouds ─────────────────────────────────────────────────
    _profile(
        "arcee",
        WIRE_OPENAI_CHAT,
        "https://api.arcee.ai/api/v1",
        ("ARCEEAI_API_KEY",),
        base_url_env="ARCEE_BASE_URL",
        display_name="Arcee AI",
        description="Arcee AI OpenAI-compatible endpoint",
        aliases=("arcee-ai", "arceeai"),
        presets={"strong": "virtuoso-large", "cheap": "virtuoso-medium"},
        fallback_models=("virtuoso-large", "virtuoso-medium"),
    ),
    _profile(
        "deepinfra",
        WIRE_OPENAI_CHAT,
        "https://api.deepinfra.com/v1/openai",
        ("DEEPINFRA_API_KEY",),
        base_url_env="DEEPINFRA_BASE_URL",
        display_name="DeepInfra",
        description="DeepInfra 100+ open models",
        signup_url="https://deepinfra.com/dash/api_keys",
        aliases=("deep-infra",),
        supports_vision=True,
        presets={
            "strong": "deepseek-ai/DeepSeek-V4-Pro",
            "cheap": "deepseek-ai/DeepSeek-V4-Flash",
        },
        fallback_models=("deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Flash"),
    ),
    _profile(
        "fireworks",
        WIRE_OPENAI_CHAT,
        "https://api.fireworks.ai/inference/v1",
        ("FIREWORKS_API_KEY",),
        display_name="Fireworks AI",
        description="Fireworks OpenAI-compatible direct model API",
        signup_url="https://app.fireworks.ai/settings/users/api-keys",
        aliases=("fireworks-ai", "fw"),
        default_headers={"HTTP-Referer": "https://wenyi.app", "X-Title": "Wenyi"},
        presets={
            "strong": "accounts/fireworks/models/kimi-k2p7-code",
            "cheap": "accounts/fireworks/models/glm-5p2",
        },
        fallback_models=(
            "accounts/fireworks/models/kimi-k2p6",
            "accounts/fireworks/models/glm-5p2",
        ),
        reasoning=_fireworks_reasoning,
    ),
    _profile(
        "gmi",
        WIRE_OPENAI_CHAT,
        "https://api.gmi-serving.com/v1",
        ("GMI_API_KEY",),
        base_url_env="GMI_BASE_URL",
        display_name="GMI Cloud",
        description="GMI Cloud multi-model direct API (slash-form model IDs)",
        signup_url="https://www.gmicloud.ai/",
        aliases=("gmi-cloud",),
        presets={"strong": "deepseek-ai/DeepSeek-V3.2", "cheap": "google/gemini-3.1-flash-lite"},
        fallback_models=("deepseek-ai/DeepSeek-V3.2", "google/gemini-3.1-flash-lite"),
    ),
    _profile(
        "nebius-token-factory",
        WIRE_OPENAI_CHAT,
        "https://api.tokenfactory.nebius.com/v1",
        ("NEBIUS_API_KEY", "NEBIUS_TOKEN_FACTORY_API_KEY"),
        base_url_env="NEBIUS_BASE_URL",
        display_name="Nebius Token Factory",
        description="Nebius Token Factory inference",
        signup_url="https://tokenfactory.nebius.com/",
        aliases=("nebius", "token-factory"),
        models_url="https://api.tokenfactory.nebius.com/v1/models?verbose=true",
        default_effort="medium",
        presets={
            "strong": "deepseek-ai/DeepSeek-V4-Pro",
            "cheap": "NousResearch/Hermes-4-70B",
        },
        fallback_models=("deepseek-ai/DeepSeek-V4-Pro", "NousResearch/Hermes-4-70B"),
        reasoning=_nebius_reasoning,
    ),
    _profile(
        "novita",
        WIRE_OPENAI_CHAT,
        "https://api.novita.ai/openai/v1",
        ("NOVITA_API_KEY",),
        base_url_env="NOVITA_BASE_URL",
        display_name="NovitaAI",
        description="NovitaAI inference cloud",
        signup_url="https://novita.ai/settings/key-management",
        aliases=("novita-ai",),
        presets={"strong": "moonshotai/kimi-k2.5", "cheap": "deepseek/deepseek-v3-0324"},
        fallback_models=("moonshotai/kimi-k2.5", "deepseek/deepseek-v3-0324"),
    ),
    _profile(
        "nvidia",
        WIRE_OPENAI_CHAT,
        "https://integrate.api.nvidia.com/v1",
        ("NVIDIA_API_KEY",),
        base_url_env="NVIDIA_BASE_URL",
        display_name="NVIDIA NIM",
        description="NVIDIA NIM accelerated inference",
        signup_url="https://build.nvidia.com/",
        aliases=("nim", "nvidia-nim"),
        default_max_tokens=16384,
        presets={
            "strong": "nvidia/llama-3.3-70b-instruct",
            "cheap": "nvidia/llama-3.1-nemotron-70b-instruct",
        },
        fallback_models=(
            "nvidia/llama-3.3-70b-instruct",
            "nvidia/llama-3.1-nemotron-70b-instruct",
        ),
    ),
    _profile(
        "ollama-cloud",
        WIRE_OPENAI_CHAT,
        "https://ollama.com/v1",
        ("OLLAMA_API_KEY",),
        base_url_env="OLLAMA_BASE_URL",
        display_name="Ollama Cloud",
        description="Ollama Cloud hosted models",
        presets={"strong": "nemotron-3-nano:30b", "cheap": "nemotron-3-nano:30b"},
        fallback_models=("nemotron-3-nano:30b",),
        reasoning=_ollama_cloud_reasoning,
    ),
    _profile(
        "meta-ai",
        WIRE_RESPONSES,
        "https://api.meta.ai/v1",
        ("MODEL_API_KEY", "META_API_KEY"),
        base_url_env="META_BASE_URL",
        display_name="Meta Model API",
        description="Meta Muse Spark family via the Responses API",
        signup_url="https://developer.meta.com/ai/",
        aliases=("meta", "muse"),
        supports_vision=True,
        default_max_tokens=16384,
        default_effort="medium",
        presets={"strong": "muse-spark-1.2", "cheap": "muse-spark-1.2-contributor"},
        fallback_models=("muse-spark-1.2", "muse-spark-1.2-contributor"),
        reasoning=_meta_reasoning,
    ),
    _profile(
        "actual",
        WIRE_OPENAI_CHAT,
        "https://api.actual.inc/v1",
        ("ACTUAL_API_KEY",),
        base_url_env="ACTUAL_BASE_URL",
        display_name="Actual Computer",
        description="Actual Computer hosted or local OpenAI-compatible inference",
        signup_url="https://actual.inc",
        aliases=("actual-computer", "aci"),
        default_effort="medium",
        presets={"strong": "glm-5.2", "cheap": "glm-5.2"},
        fallback_models=("glm-5.2",),
        reasoning=_top_level_effort(effort.OPENAI_COMPAT_WIRE_EFFORTS, disabled="none"),
    ),
    _profile(
        "azure-foundry",
        WIRE_OPENAI_CHAT,
        None,
        ("AZURE_FOUNDRY_API_KEY",),
        base_url_env="AZURE_FOUNDRY_BASE_URL",
        display_name="Azure Foundry",
        description="Microsoft Foundry OpenAI-compatible endpoint (user-supplied base URL)",
        signup_url="https://ai.azure.com/",
        aliases=("azure", "azure-ai-foundry"),
        presets={"strong": "gpt-5.4", "cheap": "gpt-5.4-mini"},
        fallback_models=("gpt-5.4", "gpt-5.4-mini"),
    ),
    # ── Local and self-hosted OpenAI-compatible endpoints ────────────────────────────
    _profile(
        "openai-compatible",
        WIRE_OPENAI_CHAT,
        None,
        (),
        base_url_env="OPENAI_COMPATIBLE_BASE_URL",
        display_name="Custom endpoint",
        description="Any OpenAI-compatible endpoint (Ollama, vLLM, llama.cpp, LM Studio)",
        aliases=("custom", "llamacpp", "llama.cpp", "llama-cpp"),
        requires_api_key=False,
        default_effort="medium",
        reasoning=_top_level_effort(effort.OPENAI_COMPAT_WIRE_EFFORTS, disabled="none"),
    ),
    # ── Subscription logins ──────────────────────────────────────────────────────────
    _profile(
        "openai-codex",
        WIRE_RESPONSES,
        "https://chatgpt.com/backend-api/codex",
        (),
        display_name="ChatGPT (Codex)",
        description="ChatGPT subscription through the Codex Responses endpoint",
        signup_url="https://chatgpt.com/",
        aliases=("codex", "chatgpt"),
        auth=AUTH_OAUTH_DEVICE,
        requires_api_key=False,
        supports_model_listing=True,
        supports_prompt_cache_key=True,
        default_headers={"originator": "codex_cli_rs"},
        default_body={"store": False, "include": ["reasoning.encrypted_content"]},
        default_effort="high",
        fallback_models=(),
        reasoning=_codex_reasoning,
        subscription="codex",
        notes="Streaming-only Responses endpoint; rejects max_output_tokens.",
    ),
    _profile(
        "copilot",
        WIRE_OPENAI_CHAT,
        "https://api.githubcopilot.com",
        ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        base_url_env="COPILOT_API_BASE_URL",
        display_name="GitHub Copilot",
        description="GitHub Copilot / GitHub Models chat completions",
        signup_url="https://github.com/features/copilot",
        aliases=("github-copilot", "github-models"),
        auth=AUTH_COPILOT,
        default_effort="medium",
        presets={"strong": "gpt-5.4", "cheap": "gpt-5.4-mini"},
        fallback_models=("gpt-5.4", "gpt-5.4-mini"),
        subscription="copilot",
        notes="GPT-5+ uses Responses except gpt-5-mini; Claude and other families use Chat Completions.",
    ),
    _profile(
        "antigravity",
        WIRE_GEMINI_CLOUDCODE,
        "https://cloudcode-pa.googleapis.com",
        ("WENYI_ANTIGRAVITY_OAUTH",),
        display_name="Google Antigravity",
        description="Google Antigravity / Gemini model access through Cloud Code",
        signup_url="https://antigravity.google/",
        aliases=("google-antigravity",),
        auth=AUTH_OAUTH_PKCE,
        default_headers=_antigravity_headers(),
        default_max_tokens=65536,
        presets={
            "strong": "gemini-3.1-pro",
            "cheap": "gemini-3.7-flash",
            "fast": "gemini-3.7-flash",
        },
        fallback_models=("gemini-3.1-pro", "gemini-3.7-flash"),
        subscription="antigravity",
        notes="Reasoning tier is part of the model ID (for example gemini-3.7-flash-high).",
    ),
    _profile(
        "vertex",
        WIRE_OPENAI_CHAT,
        "https://aiplatform.googleapis.com/v1",
        (),
        base_url_env="VERTEX_BASE_URL",
        display_name="Google Vertex AI",
        description="Gemini through Vertex AI's OpenAI-compatible endpoint (ADC credentials)",
        signup_url="https://cloud.google.com/vertex-ai",
        aliases=("vertex-ai", "google-vertex"),
        auth=AUTH_VERTEX,
        requires_api_key=False,
        supports_model_listing=False,
        presets={"strong": "google/gemini-3.6-flash", "cheap": "google/gemini-3.6-flash"},
        fallback_models=("google/gemini-3.6-flash",),
    ),
)


def profile_index() -> dict[str, ProviderProfile]:
    """Index profiles by kind and alias, rejecting duplicates."""
    index: dict[str, ProviderProfile] = {}
    for profile in PROFILES:
        for key in (profile.kind, *profile.aliases):
            if key in index:
                raise ValueError(f"Duplicate provider profile or alias: {key}")
            index[key] = profile
    return index


PROFILE_INDEX: Mapping[str, ProviderProfile] = profile_index()


def get_profile(kind: str) -> ProviderProfile:
    """Return the profile for a kind or alias."""
    try:
        return PROFILE_INDEX[kind]
    except KeyError:
        raise ValueError(
            f"Unknown provider: {kind}; available: {', '.join(sorted(p.kind for p in PROFILES))}"
        ) from None


__all__ = [
    "AUTH_API_KEY",
    "AUTH_COPILOT",
    "AUTH_OAUTH_DEVICE",
    "AUTH_OAUTH_IMPORT",
    "AUTH_OAUTH_PKCE",
    "AUTH_OAUTH_USER_CODE",
    "AUTH_VERTEX",
    "PROFILES",
    "PROFILE_INDEX",
    "ProviderProfile",
    "ReasoningRequest",
    "WIRE_ANTHROPIC",
    "WIRE_GEMINI_CLOUDCODE",
    "WIRE_GEMINI_NATIVE",
    "WIRE_OPENAI_CHAT",
    "WIRE_RESPONSES",
    "get_profile",
    "profile_index",
]
