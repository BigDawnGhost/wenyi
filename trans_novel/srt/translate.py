"""字幕翻译：滑窗批次 + 高并发，复用 Wenyi strong 档，无术语库。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..assemble.srt_writer import default_srt_out_paths, write_srt_outputs
from ..config import Config
from ..ingest.srt_reader import parse_srt
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from .store import STATUS_DONE, SrtRunStore

ProgressFn = Callable[[int, int, str], None]

BATCH_SIZE = 20
OVERLAP_SIZE = 10
MAX_CONCURRENT = 100
RETRY_LIMIT = 3

_JSON_OBJECT = re.compile(r"(\{.*\})", re.DOTALL)


def _target_language_name(code: str) -> str:
    normalized = (code or "zh").strip().lower().replace("_", "-")
    if normalized.startswith("zh"):
        return "Simplified Chinese"
    return code or "Simplified Chinese"


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
) -> dict[str, str] | None:
    indices = sorted(int(key) for key in batch)
    start_idx, end_idx = indices[0], indices[-1]
    user = (
        f"You are a professional movie subtitle translator. Translate the following "
        f"subtitles to {target_language}. Maintain the original tone and flow. "
        f"Return ONLY a valid JSON object mapping subtitle index strings to translations. "
        f"Target range: {start_idx} to {end_idx}.\n"
        f"Input:\n{json.dumps(batch, ensure_ascii=False)}"
    )
    messages = [
        {"role": "system", "content": "You are a helpful assistant that outputs JSON."},
        {"role": "user", "content": user},
    ]
    for _attempt in range(RETRY_LIMIT):
        try:
            raw = client.complete(
                messages,
                tier="strong",
                json_mode=True,
                stage="srt_translate",
            )
            parsed = _parse_batch_json(raw)
            if parsed is not None:
                return parsed
        except Exception:  # noqa: BLE001 - 批次失败由上层补漏/重试消化
            continue
    return None


def _translate_single(client: LLMClient, text: str, *, target_language: str) -> str:
    if not text.strip():
        return ""
    messages = [
        {
            "role": "user",
            "content": f"Translate this movie subtitle to {target_language}: {text!r}",
        }
    ]
    try:
        raw = client.complete(messages, tier="strong", stage="srt_translate_fallback")
        return raw.strip().strip('"')
    except Exception:  # noqa: BLE001 - 单条失败回退原文
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
    """把 client 尚未落盘的用量增量合并到 usage.json，返回 (cumulative, new_checkpoint)。"""
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
    """翻译 SRT：高并发滑窗、轻量 state 续跑，写出 output 下的字幕文件。"""
    cues = parse_srt(source_path)
    write_mono = config.output.mono if mono is None else mono
    write_bilingual = config.output.bilingual if bilingual is None else bilingual
    if not write_mono and not write_bilingual:
        write_mono = True

    store = SrtRunStore.for_source(config.state_dir, source_path)
    store.ensure_manifest(
        source_path,
        cue_count=len(cues),
        source_lang=config.source_lang or "auto",
        target_lang=config.target_lang or "zh",
        batch_size=BATCH_SIZE,
        overlap_size=OVERLAP_SIZE,
        max_concurrent=MAX_CONCURRENT,
    )
    cue_rows = store.ensure_cues([(c.index, c.timestamp, c.text) for c in cues])

    llm = client or build_client(config)
    llm.validate_credentials()
    llm.set_event_sink(store.log_event)
    usage_checkpoint = llm.usage_summary()

    store.log_event(
        "srt_run_started",
        source_path=os.path.abspath(source_path),
        cue_count=len(cues),
        run_dir=store.run_dir,
    )

    source_map = {cue.index: cue.text for cue in cues}
    segment_items = list(source_map.items())
    step = BATCH_SIZE - OVERLAP_SIZE
    target_language = _target_language_name(config.target_lang)

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

    # 已缓存批次先合并进内存
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
        progress(0, max(total, 1), "翻译字幕…")

    def run_job(
        job: tuple[int, dict[str, str], bool, bool],
    ) -> tuple[int, dict[str, str] | None, bool, bool]:
        start, batch, is_first, is_last = job
        result = _translate_batch(llm, batch, target_language=target_language)
        return start, result, is_first, is_last

    if pending:
        workers = min(MAX_CONCURRENT, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as executor:
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
                    progress(done, max(total, 1), "翻译字幕…")
        usage_cumulative, usage_checkpoint = _flush_usage(
            store, llm, usage_checkpoint, scope="srt_batches"
        )
    else:
        usage_cumulative = store.load_usage() or llm.usage_summary()

    missing = [key for key in source_map if key not in final_translations]
    if missing:
        store.log_event("srt_fallback", missing_count=len(missing))
        if progress:
            progress(0, len(missing), "补漏字幕…")
        with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT, len(missing))) as executor:
            future_map = {
                executor.submit(
                    _translate_single,
                    llm,
                    source_map[key],
                    target_language=target_language,
                ): key
                for key in missing
            }
            finished = 0
            for future in as_completed(future_map):
                key = future_map[future]
                final_translations[key] = future.result() or source_map[key]
                finished += 1
                if progress:
                    progress(finished, len(missing), "补漏字幕…")
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
    )
    outputs = write_srt_outputs(
        cues,
        final_translations,
        mono_path=mono_path,
        bilingual_path=bilingual_path,
    )

    usage_cumulative, _ = _flush_usage(store, llm, usage_checkpoint, scope="srt_finish")
    # 即使本轮无 token 增量（如 FakeClient），也落盘空壳，便于续跑合并与 CLI 对账
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
