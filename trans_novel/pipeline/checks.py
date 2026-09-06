"""Low-cost deterministic checks for paragraph alignment and suspicious length ratios.
These token-free checks complement model review.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LengthFlag:
    index: int
    ratio: float
    reason: str  # "too_short" | "too_long" | "empty"


def length_flags(
    sources: list[str],
    targets: list[str],
    *,
    too_short: float = 0.30,
    too_long: float = 3.0,
) -> list[LengthFlag]:
    """Flag suspicious translation/source character ratios.
    Very small ratios may indicate omissions, while very large ratios may indicate added or
    runaway output. Use permissive thresholds to catch obvious anomalies with fewer false
    positives.
    """
    flags: list[LengthFlag] = []
    for i, (s, t) in enumerate(zip(sources, targets)):
        s_len = len(s.strip())
        t_len = len((t or "").strip())
        if s_len == 0:
            continue
        if t_len == 0:
            flags.append(LengthFlag(i, 0.0, "empty"))
            continue
        ratio = t_len / s_len
        if ratio < too_short:
            flags.append(LengthFlag(i, ratio, "too_short"))
        elif ratio > too_long:
            flags.append(LengthFlag(i, ratio, "too_long"))
    return flags
