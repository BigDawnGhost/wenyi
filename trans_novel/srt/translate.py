"""Concurrent sliding-window subtitle translation using the strong tier without a glossary."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..assemble.srt_writer import default_srt_out_paths, write_srt_outputs
from ..config import Config
from ..i18n.languages import require_language
from ..i18n.prompts import render
from ..i18n.resources import prompt_fingerprint
from ..ingest.srt_reader import parse_srt
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from ..timing import RunTimer
from .store import STATUS_DONE, SrtRunStore

ProgressFn = Callable[[int, int, str], None]

BATCH_SIZE = 20
OVERLAP_SIZE = 10
MAX_CONCURRENT = 100
RETRY_LIMIT = 3

_JSON_OBJECT = re.compile(r"(\{.*\})", re.DOTALL)


def _parse_batch_json(text: str) -> dict[str, str] | None:
    match = _JSON_OBJECT.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, str):
            out[str(key)] = value
    return out or None


def _translate_batch(
    client: LLMClient,
    batch: dict[str, str],
    *,
    target_language: str,
    source_language: str = "auto",
) -> dict[str, str] | None:
    indices = sorted(int(key) for key in batch)
    start_idx, end_idx = indices[0], indices[-1]
    user = render(
        "srt_batch_user",
        src=source_language,
        tgt=target_language,
        start_idx=start_idx,
        end_idx=end_idx,
        batch_json=json.dumps(batch, ensure_ascii=False),
    )
    messages = [
        {
            "role": "system",
            "content": render("srt_batch_system", src=source_language, tgt=target_language),
        },
        {"role": "user", "content": user},
    ]
    for _attempt in range(RETRY_LIMIT):
        try:
            raw = client.complete(
                messages,
                operation="srt.translate",
                json_mode=True,
            )
            parsed = _parse_batch_json(raw)
            if parsed is not None:
                return parsed
        except Exception:  # noqa: BLE001 - Higher-level gap filling/retries handle batch failures.
            continue
    return None


def _translate_single(
    client: LLMClient, text: str, *, target_language: str, source_language: str = "auto"
) -> str:
    if not text.strip():
        return ""
    messages = [
        {
            "role": "system",
            "content": render("srt_single_system", src=source_language, tgt=target_language),
        },
        {
            "role": "user",
            "content": render(
                "srt_single_user", src=source_language, tgt=target_language, source=repr(text)
            ),
        },
    ]
    try:
        raw = client.complete(messages, operation="srt.translate")
        return raw.strip().strip('"')
    except Exception:  # noqa: BLE001 - Individual cue failures fall back to source text.
        return text


def _merge_batch_result(
    final_translations: dict[str, str],
    segment_items: list[tuple[str, str]],
    batch_result: dict[str, str],
    *,
    start_pos: int,
    is_first: bool,
    is_last: bool,
) -> None:
    padding = OVERLAP_SIZE // 2
    active_start = 0 if is_first else padding
    active_end = BATCH_SIZE if is_last else (BATCH_SIZE - padding)
    current = segment_items[start_pos : start_pos + BATCH_SIZE]
    for rel_idx in range(active_start, min(active_end, len(current))):
        key, _source = current[rel_idx]
        if key in batch_result:
            final_translations[key] = batch_result[key]


def _flush_usage(
    store: SrtRunStore,
    client: LLMClient,
    checkpoint: dict[str, Any],
    *,
    scope: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge unpersisted client usage into usage.json; return cumulative usage and the new
    checkpoint.
    """
    current = client.usage_summary()
    increment = usage_delta(current, checkpoint)
    accumulated = store.load_usage() or {
        "totals": {},
        "by_tier": {},
        "by_stage": {},
    }
    if not increment["totals"]["calls"]:
        return merge_usage_summaries(accumulated, increment), current
    cumulative = merge_usage_summaries(accumulated, increment)
    store.save_usage(cumulative)
    store.log_event(
        "usage_summary",
        scope=scope,
        increment=increment,
        cumulative=cumulative,
    )
    return cumulative, current


def translate_srt(
    source_path: str,
    config: Config,
    *,
    client: LLMClient | None = None,
    out: str | None = None,
    mono: bool | None = None,
    bilingual: bool | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Translate SRT with concurrent windows, lightweight resume state and output subtitle
    files.
    """
    with RunTimer("srt.translate") as timer:
        source_language = require_language(config.source_lang, allow_auto=True)
        target_language = require_language(config.target_lang)
        if source_language == target_language:
            raise ValueError(
                f"Source and target languages are identical ({target_language}); no translation is needed."
            )
        cues = parse_srt(source_path)
        write_mono = config.output.mono if mono is None else mono
        write_bilingual = config.output.bilingual if bilingual is None else bilingual
        if not write_mono and not write_bilingual:
            write_mono = True

        store = SrtRunStore.for_source(config.state_dir, source_path, target_language)
        store.ensure_manifest(
            source_path,
            cue_count=len(cues),
            source_lang=config.source_lang or "auto",
            target_lang=config.target_lang or "zh",
            batch_size=BATCH_SIZE,
            overlap_size=OVERLAP_SIZE,
            max_concurrent=MAX_CONCURRENT,
        )
        timer.store = store
        cue_rows = store.ensure_cues([(c.index, c.timestamp, c.text) for c in cues])

        llm = client or build_client(config)
        from ..llm.routing import inference_snapshot
        from ..llm.usage import validate_usage

        validate_usage(store.load_usage())
        llm.validate_credentials(("srt.translate",))
        llm.set_event_sink(store.log_event)
        usage_checkpoint = llm.usage_summary()

        store.log_event(
            "srt_run_started",
            source_path=os.path.abspath(source_path),
            cue_count=len(cues),
            run_dir=store.run_dir,
            prompt_fingerprint=prompt_fingerprint(),
            inference=inference_snapshot(config.llm, ("srt.translate",)),
        )

        source_map = {cue.index: cue.text for cue in cues}
        segment_items = list(source_map.items())
        step = BATCH_SIZE - OVERLAP_SIZE

        jobs: list[tuple[int, dict[str, str], bool, bool]] = []
        for start in range(0, len(segment_items), step):
            batch = dict(segment_items[start : start + BATCH_SIZE])
            jobs.append(
                (
                    start,
                    batch,
                    start == 0,
                    start + BATCH_SIZE >= len(segment_items),
                )
            )

        final_translations = store.translations_from_cues(cue_rows)
        pending = [
            job
            for job in jobs
            if store.load_batch(job[0]) is None
            and not _batch_active_complete(final_translations, segment_items, job)
        ]

        # Merge cached batches into memory first.
        for start, _batch, is_first, is_last in jobs:
            cached = store.load_batch(start)
            if cached is not None:
                _merge_batch_result(
                    final_translations,
                    segment_items,
                    cached,
                    start_pos=start,
                    is_first=is_first,
                    is_last=is_last,
                )

        total = len(pending)
        done = 0
        if progress:
            progress(0, max(total, 1), "Translating subtitles…")

        def run_job(
            job: tuple[int, dict[str, str], bool, bool],
        ) -> tuple[int, dict[str, str] | None, bool, bool]:
            start, batch, is_first, is_last = job
            result = _translate_batch(
                llm, batch, target_language=target_language, source_language=source_language
            )
            return start, result, is_first, is_last

        if pending:
            workers = min(MAX_CONCURRENT, len(pending))
            with llm.interrupt_scope(), ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(run_job, job): job[0] for job in pending}
                for future in as_completed(futures):
                    start, result, is_first, is_last = future.result()
                    if result:
                        store.save_batch(start, result)
                        _merge_batch_result(
                            final_translations,
                            segment_items,
                            result,
                            start_pos=start,
                            is_first=is_first,
                            is_last=is_last,
                        )
                        store.apply_translations(cue_rows, final_translations)
                        store.save_cues(cue_rows)
                        store.log_event("srt_batch_done", batch_start=start, size=len(result))
                    else:
                        store.log_event("srt_batch_failed", batch_start=start)
                    done += 1
                    if progress:
                        progress(done, max(total, 1), "Translating subtitles…")
            usage_cumulative, usage_checkpoint = _flush_usage(
                store, llm, usage_checkpoint, scope="srt_batches"
            )
        else:
            usage_cumulative = store.load_usage() or llm.usage_summary()

        missing = [key for key in source_map if key not in final_translations]
        if missing:
            store.log_event("srt_fallback", missing_count=len(missing))
            if progress:
                progress(0, len(missing), "Translating missing subtitles…")
            with (
                llm.interrupt_scope(),
                ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT, len(missing))) as executor,
            ):
                future_map = {
                    executor.submit(
                        _translate_single,
                        llm,
                        source_map[key],
                        target_language=target_language,
                        source_language=source_language,
                    ): key
                    for key in missing
                }
                finished = 0
                for future in as_completed(future_map):
                    key = future_map[future]
                    final_translations[key] = future.result() or source_map[key]
                    finished += 1
                    if progress:
                        progress(finished, len(missing), "Translating missing subtitles…")
            store.apply_translations(cue_rows, final_translations)
            store.save_cues(cue_rows)
            usage_cumulative, usage_checkpoint = _flush_usage(
                store, llm, usage_checkpoint, scope="srt_fallback"
            )

        store.apply_translations(cue_rows, final_translations, status=STATUS_DONE)
        store.save_cues(cue_rows)
        done_count = sum(1 for row in cue_rows.values() if row.get("status") == STATUS_DONE)
        store.update_manifest(done_count=done_count, status="done", cue_count=len(cues))

        mono_path, bilingual_path = default_srt_out_paths(
            source_path,
            out=out,
            mono=write_mono,
            bilingual=write_bilingual,
            target_lang=target_language,
        )
        outputs = write_srt_outputs(
            cues,
            final_translations,
            mono_path=mono_path,
            bilingual_path=bilingual_path,
        )

        usage_cumulative, _ = _flush_usage(store, llm, usage_checkpoint, scope="srt_finish")
        # Persist even empty usage, such as FakeClient runs, for resume merging and CLI accounting.
        if not os.path.isfile(store.usage_path):
            store.save_usage(usage_cumulative)
        store.log_event(
            "srt_run_finished",
            translated=len(final_translations),
            cue_count=len(cues),
            outputs=outputs,
        )
        return {
            "outputs": outputs,
            "run_dir": store.run_dir,
            "cue_count": len(cues),
            "translated": len(final_translations),
            "usage": store.load_usage() or usage_cumulative,
        }


def _batch_active_complete(
    translations: dict[str, str],
    segment_items: list[tuple[str, str]],
    job: tuple[int, dict[str, str], bool, bool],
) -> bool:
    start, _batch, is_first, is_last = job
    padding = OVERLAP_SIZE // 2
    active_start = 0 if is_first else padding
    active_end = BATCH_SIZE if is_last else (BATCH_SIZE - padding)
    current = segment_items[start : start + BATCH_SIZE]
    for rel_idx in range(active_start, min(active_end, len(current))):
        key, _source = current[rel_idx]
        if key not in translations:
            return False
    return True
