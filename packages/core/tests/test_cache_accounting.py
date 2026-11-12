"""Provider-cache partitions and backwards-compatible ledger accounting."""

import copy
import json
from types import SimpleNamespace

import pytest
from wenyi_core.llm.providers._openai_compatible import normalize_openai_usage
from wenyi_core.llm.providers.deepseek import normalize_deepseek_usage
from wenyi_core.llm.usage import (
    UsageTracker,
    empty_usage,
    merge_usage_summaries,
    usage_delta,
    validate_usage,
)


def summary(usage):
    tracker = UsageTracker()
    tracker.record("cheap", normalize_openai_usage(usage), "review.scan")
    return tracker.summary()


def test_inclusive_read_write_and_ordinary_tokens_are_separate():
    totals = summary(
        {
            "prompt_tokens": 4_727_385,
            "completion_tokens": 28_584,
            "prompt_tokens_details": {"cached_tokens": 46_600},
            "cache_creation_input_tokens": 4_680_593,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 4_680_593,
            },
        }
    )["totals"]
    assert totals["prompt_tokens"] == 4_727_385
    assert totals["cache_hit_tokens"] == 46_600
    assert totals["cache_write_tokens"] == 4_680_593
    assert totals["uncached_tokens"] == 192
    assert totals["cache_unknown_tokens"] == 0
    assert totals["cache_write_1h_tokens"] == 4_680_593
    assert totals["cache_write_5m_tokens"] == 0
    assert totals["cache_write_5m_reported_calls"] == 1
    assert totals["cache_hit_rate"] == round(46_600 / 4_727_385, 4)


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": None}])
def test_absent_and_null_cache_fields_remain_unknown(details):
    totals = summary({"prompt_tokens": 100, "prompt_tokens_details": details})["totals"]
    assert totals["cache_unknown_tokens"] == 100
    assert totals["cache_hit_reported_calls"] == 0
    assert totals["cache_write_reported_calls"] == 0
    assert totals["cache_hit_rate"] is None


def test_explicit_zero_is_distinct_from_absence():
    totals = summary(
        {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        }
    )["totals"]
    assert totals["uncached_tokens"] == 100
    assert totals["cache_unknown_tokens"] == 0
    assert totals["cache_hit_reported_calls"] == 1
    assert totals["cache_write_reported_calls"] == 1
    assert totals["cache_write_1h_reported_calls"] == 0
    assert totals["cache_hit_rate"] == 0


def test_read_without_write_details_does_not_invent_ordinary_input():
    totals = summary({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}})[
        "totals"
    ]
    assert totals["cache_hit_tokens"] == 40
    assert totals["uncached_tokens"] == 0
    assert totals["cache_unknown_tokens"] == 60
    assert totals["cache_hit_rate"] == 0.4


@pytest.mark.parametrize(
    "details",
    [
        {"cached_tokens": 120, "cache_write_tokens": 0},
        {"cached_tokens": 60, "cache_write_tokens": 60},
    ],
)
def test_inconsistent_cache_totals_are_not_counted(details):
    totals = summary({"prompt_tokens": 100, "prompt_tokens_details": details})["totals"]
    assert totals["prompt_tokens"] == 100
    assert totals["cache_unknown_tokens"] == 100
    assert totals["cache_hit_rate"] is None


def test_mixed_coverage_uses_all_input_as_denominator():
    known = summary(
        {
            "prompt_tokens": 284_683,
            "prompt_tokens_details": {"cached_tokens": 111_247, "cache_write_tokens": 0},
        }
    )
    unknown = summary({"prompt_tokens": 737_609})
    totals = merge_usage_summaries(known, unknown)["totals"]
    assert totals["cache_unknown_tokens"] == 737_609
    assert totals["cache_hit_rate"] == round(111_247 / 1_022_292, 4)
    assert totals["cache_hit_reported_calls"] == 1
    assert totals["calls"] == 2


def test_missing_read_is_not_inferred_from_a_reported_miss():
    tracker = UsageTracker()
    tracker.record(
        "cheap",
        normalize_deepseek_usage({"prompt_tokens": 100, "prompt_cache_miss_tokens": 50}),
    )
    totals = tracker.summary()["totals"]
    assert totals["uncached_tokens"] == 50
    assert totals["cache_unknown_tokens"] == 50
    assert totals["cache_hit_reported_calls"] == 0
    assert totals["cache_hit_rate"] is None


def test_sdk_objects_and_ttl_subtotals():
    totals = summary(
        SimpleNamespace(
            prompt_tokens=100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10, cache_write_tokens=80),
            cache_creation=SimpleNamespace(
                ephemeral_5m_input_tokens=30, ephemeral_1h_input_tokens=50
            ),
        )
    )["totals"]
    assert totals["cache_write_tokens"] == 80
    assert totals["cache_write_5m_tokens"] == 30
    assert totals["cache_write_1h_tokens"] == 50
    assert totals["uncached_tokens"] == 10


def test_v2_ledger_is_upgraded_without_reclassifying_old_misses(tmp_path):
    slot = {
        "calls": 2,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "cache_hit_tokens": 10,
        "cache_miss_tokens": 70,
        "cache_hit_rate": 0.125,
    }
    old = {
        "schema_version": 2,
        "totals": slot,
        "by_tier": {"cheap": slot},
        "by_stage": {"review.scan": slot},
        "by_provider": {"provider": slot},
        "by_model": {"model": slot},
        "labels": {"model": "Historical model"},
    }
    before = copy.deepcopy(old)
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(old))
    migrated = validate_usage(json.loads(path.read_text()))
    assert old == before
    assert json.loads(path.read_text()) == old
    assert migrated["schema_version"] == 3
    assert migrated["labels"] == old["labels"]
    assert migrated["totals"]["cache_miss_tokens"] == 70
    assert migrated["totals"]["cache_unknown_tokens"] == 90
    assert migrated["totals"]["uncached_tokens"] == 0
    assert migrated["totals"]["cache_write_tokens"] == 0
    assert migrated["totals"]["cache_hit_rate"] == 0.1
    fresh = summary(
        {
            "prompt_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 25},
        }
    )
    combined = merge_usage_summaries(old, fresh)
    assert combined["totals"]["cache_unknown_tokens"] == 90
    assert combined["totals"]["cache_write_tokens"] == 25
    assert combined["totals"]["uncached_tokens"] == 5
    assert merge_usage_summaries(old, usage_delta(combined, old)) == combined
    assert usage_delta(combined, combined) == empty_usage() | {"labels": combined["labels"]}
    path.write_text(json.dumps(combined))
    assert validate_usage(json.loads(path.read_text())) == combined


@pytest.mark.parametrize("bad", [-1, True, 1.5, "4", "bad"])
def test_invalid_read_counters_are_not_reported_as_zero(bad):
    totals = summary({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": bad}})[
        "totals"
    ]
    assert totals["cache_hit_reported_calls"] == 0
    assert totals["cache_unknown_tokens"] == 100
    assert totals["cache_hit_rate"] is None


def test_native_openai_has_no_separate_write_bucket():
    sample = normalize_openai_usage(
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 30}},
        separate_cache_writes=False,
    )
    assert sample is not None
    assert sample.uncached_tokens == 70
    assert sample.cache_write_tokens == 0
    assert sample.cache_unknown_tokens == 0


def test_partial_ttl_counts_proven_writes_without_inventing_a_complete_total():
    totals = summary(
        {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 10},
            "cache_creation": {"ephemeral_5m_input_tokens": 50},
        }
    )["totals"]
    assert totals["cache_write_tokens"] == 50
    assert totals["uncached_reported_calls"] == 0
    assert totals["cache_unknown_tokens"] == 40
    assert totals["cache_write_1h_reported_calls"] == 0


def test_native_separate_input_dialect_is_not_mistaken_for_inclusive_totals():
    assert (
        normalize_openai_usage(
            {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30}
        )
        is None
    )


def test_inconsistent_ttl_does_not_discard_valid_input_partition():
    totals = summary(
        {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 80},
            "cache_creation": {"ephemeral_5m_input_tokens": 50, "ephemeral_1h_input_tokens": 50},
        }
    )["totals"]
    assert totals["cache_write_tokens"] == 80
    assert totals["uncached_tokens"] == 10
    assert totals["cache_write_5m_reported_calls"] == 0
    assert totals["cache_write_1h_reported_calls"] == 0


def test_cli_prints_coverage_and_missing_ttl_without_claiming_zero():
    from io import StringIO

    from rich.console import Console
    from wenyi_cli.commands.presentation import print_usage

    ledger = summary(
        {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 80},
            "cache_creation": {"ephemeral_1h_input_tokens": 80},
        }
    )
    ledger = merge_usage_summaries(ledger, summary({"prompt_tokens": 100}))
    output = StringIO()
    print_usage(Console(file=output, width=500, color_system=None), ledger)
    text = output.getvalue()
    assert "cache read / all input 5.0%" in text
    assert "write 80 / ordinary 10 / unknown 100 tok" in text
    assert "5m unknown / 1h 80 tok" in text
