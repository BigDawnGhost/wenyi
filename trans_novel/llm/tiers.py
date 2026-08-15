"""模型档位解析。"""

from __future__ import annotations

from typing import TypeVar

from ..config import validate_llm_tier

TierConfigT = TypeVar("TierConfigT")


def resolve_tier(tiers: dict[str, TierConfigT], tier: str) -> TierConfigT:
    """精确解析合法 tier；禁止未知名称和跨档位静默回退。"""
    validate_llm_tier(tier)
    try:
        return tiers[tier]
    except KeyError:
        raise KeyError(f"未配置 LLM tier：{tier}") from None
