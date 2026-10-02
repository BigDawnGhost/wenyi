"""Canonical reasoning-effort vocabulary and wire clamping.

Wenyi's internal ladder (``EFFORT_LADDER``) is wider than any single provider wire accepts.
Requested levels that a wire rejects are clamped to the nearest *weaker* supported level so a
provider-specific 400 never reaches the user and cost is never escalated by a clamp.

Only the vocabulary math lives here; the wire shape (which field carries the toggle or the
effort) stays with each provider profile.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Canonical low-to-high ordering used for nearest-level clamping.
EFFORT_LADDER: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

# Widest OpenAI-compatible wire vocabulary (OpenRouter, Nous Portal, custom relays).
OPENAI_COMPAT_WIRE_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

# OpenAI/Codex Responses: ``minimal`` is rejected by every generation; ``max`` is newest-only.
CODEX_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
CODEX_LEGACY_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh")

# xAI Responses: Grok 4.6+ accepts xhigh; older Grok tops out at high.
XAI_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
XAI_LEGACY_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

# Moonshot/Kimi: K3 accepts low/high/max with ``medium`` rounding up to its server default.
KIMI_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
KIMI_K3_EFFORTS: tuple[str, ...] = ("low", "high", "max")
KIMI_K3_OVERRIDES: Mapping[str, str] = {"medium": "high", "xhigh": "max"}

# GLM-5.2 exposes exactly high/max; GLM-5.3 widens the knob to a graded scale.
GLM_52_EFFORTS: tuple[str, ...] = ("high", "max")
GLM_53_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")

DEEPSEEK_V4_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
GLM_OR_DEEPSEEK_OVERRIDES: Mapping[str, str] = {"xhigh": "max"}

SOLAR_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
OLLAMA_CLOUD_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "max")
META_AI_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")


def clamp_effort(
    effort: str | None,
    supported: Sequence[str] | None,
    overrides: Mapping[str, str] | None = None,
) -> str | None:
    """Clamp a requested effort onto a wire's supported levels.

    A declared ``overrides`` mapping (vendor-specific aliasing) wins first. Otherwise the
    request passes through when it is supported, when the supported set is unknown or empty,
    or when it is not a recognized ladder level. Otherwise the nearest weaker supported level
    is returned; when nothing weaker exists, the weakest supported level is used. ``none`` is
    never chosen as a degradation target for an enabled request.
    """
    requested = str(effort or "").strip().lower()
    if not requested or not supported:
        return effort
    normalized = [str(level).strip().lower() for level in supported]
    known = [level for level in normalized if level in EFFORT_LADDER]
    if not known or requested in known:
        return effort
    if overrides and overrides.get(requested) in known:
        return overrides[requested]
    if requested not in EFFORT_LADDER:
        return effort
    candidates = [level for level in known if level != "none"]
    if not candidates:
        return effort
    requested_index = EFFORT_LADDER.index(requested)
    weaker = [level for level in candidates if EFFORT_LADDER.index(level) < requested_index]
    if weaker:
        return max(weaker, key=EFFORT_LADDER.index)
    return min(candidates, key=EFFORT_LADDER.index)


def effective_effort(
    *,
    enabled: bool,
    effort: str | None,
    default_effort: str | None = None,
) -> str | None:
    """Return the effort a request should carry, or None to omit the field entirely.

    ``None`` keeps the server default in charge; ``"none"`` is an explicit disable.
    """
    if not enabled:
        return "none"
    requested = str(effort or "").strip().lower()
    if requested:
        return requested
    return default_effort


__all__ = [
    "CODEX_EFFORTS",
    "CODEX_LEGACY_EFFORTS",
    "DEEPSEEK_V4_EFFORTS",
    "EFFORT_LADDER",
    "GLM_52_EFFORTS",
    "GLM_53_EFFORTS",
    "GLM_OR_DEEPSEEK_OVERRIDES",
    "KIMI_EFFORTS",
    "KIMI_K3_EFFORTS",
    "KIMI_K3_OVERRIDES",
    "META_AI_EFFORTS",
    "OLLAMA_CLOUD_EFFORTS",
    "OPENAI_COMPAT_WIRE_EFFORTS",
    "SOLAR_EFFORTS",
    "XAI_EFFORTS",
    "XAI_LEGACY_EFFORTS",
    "clamp_effort",
    "effective_effort",
]
