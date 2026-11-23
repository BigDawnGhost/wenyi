#!/usr/bin/env python3
"""Measure speed, cost, glossary health, adherence and formatting on any Wenyi run directory.

Usage:
    uv run --no-sync python scripts/benchmark_metrics.py <state-target-dir> [<state-target-dir> ...]

Each argument may be either a target state directory containing ``events.jsonl`` and
``glossary.db`` or a parent run directory containing ``state/*/targets/*``.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from wenyi_core.glossary.store import normalize_match_text, source_matches_text
from wenyi_core.i18n.languages import glossary_generic_target_words

_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_LEADING_THE_RE = re.compile(r"^the\s+")
_COLON_TAG_RE = re.compile(
    r'\b(?:said|asked|replied|answered|cried|shouted|whispered|laughed):\s*[“"]'
)
_SINGLE_QUOTE_RE = re.compile(r"(?:^|[\s,:])'[A-Z][^']{12,}'(?=$|[\s,;.)])")


def _resolve_target_dir(path: str) -> Path:
    root = Path(path).expanduser().resolve()
    if (root / "events.jsonl").is_file():
        return root
    matches = sorted(glob.glob(str(root / "state" / "*" / "targets" / "*")))
    if matches:
        return Path(matches[0])
    raise FileNotFoundError(f"Could not find Wenyi target state directory under {path}")


def _is_generic_target(target: str, generic_words: frozenset[str]) -> bool:
    if not generic_words:
        return False
    words = _WORD_RE.findall(target.casefold())
    return bool(words) and all(w.replace("’", "'") in generic_words for w in words)


def _target_key(target: str) -> str:
    return _LEADING_THE_RE.sub("", " ".join(normalize_match_text(target).split()))


def analyze_run(
    path: str,
    *,
    prompt_price_per_m: float = 0.30,
    cache_price_per_m: float = 0.007,
    completion_price_per_m: float = 1.20,
    target_lang: str = "en",
) -> dict[str, Any]:
    """Compute timing, cost, glossary adherence, and typography metrics for one run."""
    target_dir = _resolve_target_dir(path)
    events = [
        json.loads(line)
        for line in (target_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not events:
        return {"path": str(target_dir), "error": "empty events.jsonl"}

    t0 = datetime.fromisoformat(events[0]["ts"])
    t_end = datetime.fromisoformat(events[-1]["ts"])
    first_tr = next(
        (
            datetime.fromisoformat(ev["ts"])
            for ev in events
            if ev.get("event") == "llm_request_started"
            and ev.get("operation") == "translation.body"
        ),
        None,
    )

    started: dict[str, datetime] = {}
    op_minutes: dict[str, float] = collections.defaultdict(float)
    op_calls: collections.Counter[str] = collections.Counter()
    escalated_calls = 0
    prompt_tokens = 0
    cache_hit_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0
    inserted = 0
    conflicts = 0
    left_out: set[str] = set()
    post_typo_fixes = 0
    post_name_subs = 0

    for ev in events:
        kind = ev.get("event")
        if kind == "llm_request_started":
            started[str(ev.get("call_id"))] = datetime.fromisoformat(ev["ts"])
            if ev.get("thinking_boost"):
                escalated_calls += 1
        elif kind == "llm_request_completed" and str(ev.get("call_id")) in started:
            op = str(ev.get("operation") or "unknown")
            dt_min = (
                datetime.fromisoformat(ev["ts"]) - started[str(ev.get("call_id"))]
            ).total_seconds() / 60.0
            op_minutes[op] += dt_min
            op_calls[op] += 1
        elif kind == "llm_usage":
            pt = int(ev.get("prompt_tokens") or 0)
            ct = int(ev.get("completion_tokens") or 0)
            ht = int(ev.get("cache_hit_tokens") or 0)
            prompt_tokens += pt
            completion_tokens += ct
            cache_hit_tokens += ht
            cost_usd += (
                (pt - ht) * (prompt_price_per_m * 1e-6)
                + ht * (cache_price_per_m * 1e-6)
                + ct * (completion_price_per_m * 1e-6)
            )
        elif kind == "batch_glossary_extracted":
            summary = ev.get("summary") or {}
            inserted += int(summary.get("inserted") or 0)
            conflicts += int(summary.get("conflict") or 0)
        elif kind == "glossary_terms_left_out":
            left_out |= set(ev.get("added") or ())
            left_out -= set(ev.get("restored") or ())
        elif kind == "chapter_post_normalized":
            post_typo_fixes += int(ev.get("typography_fixes") or 0)
            post_name_subs += int(ev.get("name_substitutions") or 0)

    glossary_rows: list[tuple[str, str, str]] = []
    db_path = target_dir / "glossary.db"
    if db_path.is_file():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            glossary_rows = list(conn.execute("select source, target, type from glossary"))
        finally:
            conn.close()

    segments: list[dict[str, Any]] = []
    for ch_file in sorted((target_dir / "chapters").glob("ch*.json")):
        chapter = json.loads(ch_file.read_text(encoding="utf-8"))
        for seg in chapter.get("segments") or ():
            if seg.get("kind", "text") == "text" and seg.get("target"):
                segments.append(seg)

    generic_words = glossary_generic_target_words(target_lang)
    generic_entries = [
        f"{s}={t}" for s, t, _ in glossary_rows if _is_generic_target(t, generic_words)
    ]

    is_cjk_target = target_lang.lower().split("-", 1)[0] in {"zh", "ja", "ko"}
    adherence_scores: list[tuple[float, str, str]] = []
    for src, tgt, _ty in glossary_rows:
        hits = [seg for seg in segments if source_matches_text(src, seg.get("source") or "")]
        if len(hits) < 5:
            continue
        key = _target_key(tgt)
        if not key:
            continue
        if is_cjk_target:
            used = sum(
                1 for seg in hits if key in " ".join(normalize_match_text(seg["target"]).split())
            )
        else:
            pat = re.compile(rf"(?<![^\W\d_]){re.escape(key)}(?![^\W\d_])")
            used = sum(
                1
                for seg in hits
                if pat.search(" ".join(normalize_match_text(seg["target"]).split()))
            )
        adherence_scores.append((used / len(hits), src, tgt))

    colon_tags = sum(len(_COLON_TAG_RE.findall(seg["target"])) for seg in segments)
    raw_dashes = sum(1 for seg in segments if "--" in seg["target"])
    single_quotes = sum(
        1
        for seg in segments
        if '"' not in seg["target"]
        and "“" not in seg["target"]
        and _SINGLE_QUOTE_RE.search(seg["target"])
    )
    slash_verse = sum(1 for seg in segments if " / " in seg["target"])

    return {
        "path": str(target_dir),
        "segments_translated": len(segments),
        "wall_min": round((t_end - t0).total_seconds() / 60.0, 2),
        "translate_phase_min": round((t_end - first_tr).total_seconds() / 60.0, 2)
        if first_tr
        else None,
        "cost_usd": round(cost_usd, 3),
        "prompt_tokens": prompt_tokens,
        "cache_hit_pct": round(100.0 * cache_hit_tokens / prompt_tokens, 1)
        if prompt_tokens
        else 0.0,
        "completion_tokens": completion_tokens,
        "op_calls": dict(op_calls),
        "thinking_escalated_calls": escalated_calls,
        "op_minutes": {
            k: round(v, 2) for k, v in sorted(op_minutes.items(), key=lambda kv: -kv[1])
        },
        "glossary_entries": len(glossary_rows),
        "glossary_inserted": inserted,
        "glossary_conflicts": conflicts,
        "generic_target_entries": generic_entries,
        "low_adherence_entries": [
            f"{s}={t} ({score:.2f})" for score, s, t in sorted(adherence_scores) if score < 0.35
        ],
        "left_out_in_prompts": sorted(left_out),
        "post_normalized": {
            "typography_fixes": post_typo_fixes,
            "name_substitutions": post_name_subs,
        },
        "formatting_lint": {
            "colon_tags": colon_tags,
            "raw_double_dash": raw_dashes,
            "single_quote_dialogue": single_quotes,
            "slash_verse": slash_verse,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", help="Run directories or target state directories")
    parser.add_argument("--target-lang", default="en", help="Target language code (default: en)")
    args = parser.parse_args(argv)
    report = {
        str(Path(run).expanduser().resolve()): analyze_run(run, target_lang=args.target_lang)
        for run in args.runs
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
