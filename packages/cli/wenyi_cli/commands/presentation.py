"""Display workflow summaries from supplied data without accessing run state."""

from typing import Any

from rich.console import Console
from wenyi_core.timing import format_duration


def print_timing(console: Console, timing: dict[str, Any] | None) -> None:
    """Show the last invocation and accumulated execution time, including failed runs."""
    if timing is None:
        return
    last = timing["runs"][-1]
    count = len(timing["runs"])
    console.print(
        f"Time: last run {format_duration(last['elapsed_seconds'])} ({last['status']}), "
        f"cumulative {format_duration(timing['total_seconds'])} across {count} "
        f"{'run' if count == 1 else 'runs'}.",
        highlight=False,
    )


def _cache_description(slot: dict[str, Any]) -> str:
    """Describe cache coverage without reinterpreting legacy misses as ordinary input."""
    hit = slot.get("cache_hit_tokens", 0)
    prompt = slot.get("prompt_tokens", 0)
    known_read = hit or slot.get("cache_hit_reported_calls")
    rate = f"{hit / prompt:.1%}" if prompt and known_read else "unknown"
    write = slot.get("cache_write_tokens", 0)
    ordinary = slot.get("uncached_tokens", 0)
    unknown = slot.get("cache_unknown_tokens", max(0, prompt - hit - write - ordinary))

    def counter(field: str, presence: str) -> str:
        value = slot.get(field, 0)
        return f"{value:,}" if value or slot.get(presence) else "unknown"

    description = (
        f"cache read / all input {rate}"
        f" (read {f'{hit:,}' if known_read else 'unknown'}"
        f" / write {counter('cache_write_tokens', 'cache_write_reported_calls')}"
        f" / ordinary {counter('uncached_tokens', 'uncached_reported_calls')}"
        f" / unknown {unknown:,} tok)"
    )
    if slot.get("cache_write_5m_reported_calls") or slot.get("cache_write_1h_reported_calls"):
        description += (
            f" [write TTL: 5m {counter('cache_write_5m_tokens', 'cache_write_5m_reported_calls')}"
            f" / 1h {counter('cache_write_1h_tokens', 'cache_write_1h_reported_calls')} tok]"
        )
    return description


def print_usage(console: Console, usage: dict[str, Any] | None) -> None:
    """Print cumulative usage with an all-input denominator and unknown coverage."""
    usage = usage or {}
    totals = usage.get("totals") or {}
    if not totals.get("total_tokens"):
        return
    console.print(
        f"Usage (book total): {totals['total_tokens']:,} tok"
        f" (prompt {totals['prompt_tokens']:,} / completion {totals['completion_tokens']:,}), "
        f"{_cache_description(totals)}",
        markup=False,
    )
    for tier, v in sorted(usage.get("by_tier", {}).items()):
        console.print(
            f"  · {tier}: {v['total_tokens']:,} tok, {v['calls']} calls, {_cache_description(v)}",
            markup=False,
        )
    for stage, v in sorted(
        (usage.get("by_stage") or {}).items(),
        key=lambda item: -item[1]["total_tokens"],
    ):
        console.print(
            f"  · Stage {stage}: {v['total_tokens']:,} tok"
            f" (prompt {v['prompt_tokens']:,} / completion {v['completion_tokens']:,}), "
            f"{v['calls']} calls, {_cache_description(v)}",
            markup=False,
        )

    for identity, value in sorted((usage.get("by_model") or {}).items()):
        label = (usage.get("labels") or {}).get(identity, identity)
        console.print(f"  · Model {label}: {value['total_tokens']:,} tok, {value['calls']} calls")


def print_review_summary(console: Console, review_result: dict[str, Any], run_dir: str) -> None:
    """Show standalone Review completion and publication results."""
    summary = review_result["summary"]
    console.print(
        f"[bold green]Whole-book agent review complete[/]: {review_result['termination']}, "
        f"Remaining issues: {summary['issue_count']}, "
        f"suggested changes: {summary['change_count']}."
    )
    autofix_result = review_result.get("autofix") or {}
    if autofix_result.get("enabled"):
        console.print(
            f"Autofix: published {autofix_result.get('applied_segment_count', 0)} paragraphs, "
            f"failed issues: {autofix_result.get('failed_issue_count', 0)}."
        )
    else:
        console.print(
            "Review produced recommendations only; formal chapter translations are unchanged."
        )
    console.print(f"Review directory: {run_dir}")


def print_review_details(
    console: Console, review_result: dict[str, Any] | None, run_dir: str
) -> None:
    """Show review details after the full translation workflow."""
    review_result = review_result or {}
    review_summary = review_result.get("summary") or {}
    console.print(
        f"Review result: {review_result.get('termination', 'unknown')}, "
        f"issues: {review_summary.get('issue_count', 0)}, "
        f"suggested changes: {review_summary.get('change_count', 0)}."
    )
    console.print(f"Review directory: {run_dir}")


def print_preparation_summary(
    console: Console, *, chapter_count: int, digest_count: int, has_synopsis: bool, run_dir: str
) -> None:
    """Show saved preparation coverage and the next workflow command."""
    console.print(
        f"[bold green]Preparation complete[/]: Parsed {chapter_count} chapters, "
        f"prescanned {digest_count}/{chapter_count} chapters, "
        f"Whole-book synopsis{' generated' if has_synopsis else ' unavailable'}."
    )
    console.print(f"State directory: [bold]{run_dir}[/]")
    console.print("Run translate with the same source file to continue the full translation.")


def print_translation_summary(console: Console, summary: dict[str, Any]) -> None:
    """Show complete book chapter and terminology counts."""
    console.print(
        f"[bold green]Complete[/]: {summary['chapters_done']}/{summary['chapters_total']} chapters, terms: {summary['terms']}."
    )


def print_subtitle_summary(
    console: Console, *, translated: int, cue_count: int, run_dir: str
) -> None:
    """Show subtitle coverage and its independent state location."""
    console.print(
        f"[bold green]Subtitle translation complete[/]: {translated}/{cue_count} cues, "
        f"State directory: {run_dir}"
    )
