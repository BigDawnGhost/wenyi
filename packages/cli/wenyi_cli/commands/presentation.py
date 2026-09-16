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


def print_usage(console: Console, usage: dict[str, Any] | None) -> None:
    """Print cumulative book token usage and tier cache hit rates when available."""
    usage = usage or {}
    totals = usage.get("totals") or {}
    if not totals.get("total_tokens"):
        return
    console.print(
        f"Usage (book total): {totals['total_tokens']:,} tok"
        f" (prompt {totals['prompt_tokens']:,} / completion {totals['completion_tokens']:,}), "
        f"cache hit rate {totals.get('cache_hit_rate', 0.0):.1%}"
        f" (hits {totals['cache_hit_tokens']:,} / misses {totals['cache_miss_tokens']:,} tok)"
    )
    for tier, v in sorted(usage.get("by_tier", {}).items()):
        console.print(
            f"  · {tier}: {v['total_tokens']:,} tok, {v['calls']} calls, "
            f"cache hit rate {v['cache_hit_rate']:.1%}"
        )
    for stage, v in sorted(
        (usage.get("by_stage") or {}).items(),
        key=lambda item: -item[1]["total_tokens"],
    ):
        console.print(
            f"  · Stage {stage}: {v['total_tokens']:,} tok"
            f" (prompt {v['prompt_tokens']:,} / completion {v['completion_tokens']:,}), "
            f"{v['calls']} calls, cache hit rate {v['cache_hit_rate']:.1%}"
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
