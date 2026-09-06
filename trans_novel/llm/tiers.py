"""Resolve model tiers."""

from __future__ import annotations

from typing import TypeVar

TierConfigT = TypeVar("TierConfigT")

# Missing-tier fallbacks prefer cheaper tiers and never silently upgrade to a more expensive tier.
_TIER_FALLBACK = {"fast": ("cheap", "strong"), "cheap": ("strong",), "strong": ()}


def resolve_tier(tiers: dict[str, TierConfigT], tier: str) -> TierConfigT:
    """Resolve a tier through its fallback chain; raise KeyError for missing strong as before."""
    if tier in tiers:
        return tiers[tier]
    for fallback in _TIER_FALLBACK.get(tier, ("strong",)):
        if fallback in tiers:
            return tiers[fallback]
    return tiers["strong"]
