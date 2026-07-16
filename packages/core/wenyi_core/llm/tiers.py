"""模型档位解析。"""

from __future__ import annotations

from typing import TypeVar

TierConfigT = TypeVar("TierConfigT")

# 缺档回退链：向“更便宜优先”回退，绝不因缺档反而升到更贵的档
_TIER_FALLBACK = {"fast": ("cheap", "strong"), "cheap": ("strong",), "strong": ()}


def resolve_tier(tiers: dict[str, TierConfigT], tier: str) -> TierConfigT:
    """按回退链解析 tier 配置。缺 strong 时 KeyError（与旧行为一致）。"""
    if tier in tiers:
        return tiers[tier]
    for fallback in _TIER_FALLBACK.get(tier, ("strong",)):
        if fallback in tiers:
            return tiers[fallback]
    return tiers["strong"]
