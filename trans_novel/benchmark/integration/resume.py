# fmt: off

"""Integration interruption, resume, and evidence validation."""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trans_novel.benchmark.contracts import GENERATION_FIELDS
from trans_novel.benchmark.epub_check import validate_epub_triplet
from trans_novel.benchmark.integration.artifacts import (
    IntegrationError,
    IntegrationIntegrityError,
    integration_relative_path,
    integration_sha256,
    read_integration_canonical,
    read_telemetry_records,
    translator_call_count,
    usage_evidence,
    validate_candidate_paths,
    write_integration_json,
)
from trans_novel.benchmark.integration.canary import (
    canary_routes,
    model_identity,
    normalized_model,
    run_canary,
)
from trans_novel.benchmark.integration.preflight import quality_config
from trans_novel.benchmark.run import JsonlCallTelemetrySink
from trans_novel.benchmark.schema import Candidate, CandidateSpec
from trans_novel.llm.generation import GenerationOptions
from trans_novel.llm.telemetry import CallAttemptTelemetry
from trans_novel.pipeline import Application
from trans_novel.pipeline.execution import assemble_readiness_problems
from trans_novel.pipeline.state import RunStore

TRANSLATOR_OPERATIONS = frozenset({'translate.batch', 'translate.single', 'translate.heading'})

def event_rows(store: RunStore) -> list[dict[str, Any]]:
    path = Path(store.event_log_path)
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip() and isinstance(json.loads(line), dict)]
    except Exception as error:
        raise IntegrationError(f'invalid RunStore event log: {error}') from error

def parse_event_bytes(raw: bytes) -> list[dict[str, Any]]:
    try:
        rows = []
        for line in raw.decode('utf-8').splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('event is not an object')
                rows.append(value)
        return rows
    except Exception as error:
        raise IntegrationError(f'invalid RunStore event prefix: {error}') from error

def authenticated_event_prefix(store: RunStore, *, count: int, size: int, digest: str) -> list[dict[str, Any]]:
    raw = Path(store.event_log_path).read_bytes()
    if count < 0 or size < 0 or len(raw) < size:
        raise IntegrationError('event prefix boundary is beyond persisted events')
    prefix = raw[:size]
    if hashlib.sha256(prefix).hexdigest() != digest:
        raise IntegrationIntegrityError('interrupted event prefix mismatch')
    rows = parse_event_bytes(prefix)
    if len(rows) != count:
        raise IntegrationIntegrityError('interrupted event prefix count mismatch')
    return rows

def authenticated_telemetry_prefix(path: Path, *, count: int, size: int, digest: str) -> list[CallAttemptTelemetry]:
    raw = path.read_bytes()
    if count < 0 or size < 0 or len(raw) < size:
        raise IntegrationError('telemetry prefix boundary is beyond persisted attempts')
    prefix = raw[:size]
    if hashlib.sha256(prefix).hexdigest() != digest:
        raise IntegrationIntegrityError('interrupted telemetry prefix mismatch')
    try:
        rows = [CallAttemptTelemetry.model_validate(json.loads(line)) for line in prefix.decode('utf-8').splitlines() if line.strip()]
    except Exception as error:
        raise IntegrationIntegrityError('interrupted telemetry prefix is invalid') from error
    if len(rows) != count:
        raise IntegrationIntegrityError('interrupted telemetry prefix count mismatch')
    return rows

def batch_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{'chapter': int(r['chapter']), 'start': int(r['start_index']), 'count': int(r['count']), 'translate_call_count': int(r.get('translate_call_count', 1)), 'target_sha256': str(r['target_sha256'])} for r in rows if r.get('event') == 'batch_translated']

def skip_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{'chapter': int(r['chapter']), 'start': int(r['start_index']), 'count': int(r['count']), 'target_sha256': str(r.get('target_sha256', ''))} for r in rows if r.get('event') == 'batch_skipped' and r.get('reason') == 'already_translated']

def candidate_store(state_dir: Path) -> RunStore:
    candidates = [p for p in state_dir.iterdir() if p.is_dir() and (p / 'manifest.json').exists()] if state_dir.exists() else []
    if len(candidates) != 1:
        raise IntegrationError('candidate Application state root is missing or ambiguous')
    return RunStore(str(candidates[0]))

def validate_restart_prefixes(state_entry: dict[str, Any], *, candidate_store: RunStore, telemetry_path: Path) -> tuple[list[dict[str, Any]], list[CallAttemptTelemetry]]:
    required = ('boundary_event_count', 'event_prefix_size', 'event_prefix_sha256', 'first_telemetry_count', 'telemetry_prefix_size', 'telemetry_prefix_sha256', 'canary_telemetry_count')
    if any(not isinstance(state_entry.get(k), int) for k in required if k.endswith(('count', 'size'))):
        raise IntegrationError('interrupted candidate evidence is incomplete')
    interruption = state_entry.get('interruption')
    if not isinstance(interruption, dict) or not isinstance(interruption.get('batches'), list):
        raise IntegrationError('interruption identity evidence is incomplete')
    first = authenticated_event_prefix(candidate_store, count=int(state_entry.get('first_boundary_event_count', state_entry['boundary_event_count'])), size=int(state_entry.get('first_event_prefix_size', state_entry['event_prefix_size'])), digest=str(state_entry.get('first_event_prefix_sha256', state_entry['event_prefix_sha256'])))
    identities = [{'chapter': r['chapter'], 'start': r['start'], 'count': r['count']} for r in batch_rows(first)]
    if identities != interruption['batches'] or interruption.get('count') != len(identities):
        raise IntegrationIntegrityError('interruption identities do not match event prefix')
    events = authenticated_event_prefix(candidate_store, count=state_entry['boundary_event_count'], size=state_entry['event_prefix_size'], digest=state_entry['event_prefix_sha256'])
    if not isinstance(state_entry.get('before_target_hashes'), list) or batch_rows(events) != state_entry['before_target_hashes']:
        raise IntegrationIntegrityError('interruption target hashes do not match event prefix')
    first_count = state_entry['first_telemetry_count']
    telemetry = authenticated_telemetry_prefix(telemetry_path, count=first_count, size=state_entry.get('first_telemetry_prefix_size', state_entry['telemetry_prefix_size']), digest=state_entry.get('first_telemetry_prefix_sha256', state_entry['telemetry_prefix_sha256']))
    if state_entry['canary_telemetry_count'] != 2 or len(telemetry) < 2:
        raise IntegrationError('canary telemetry prefix is incomplete')
    if state_entry.get('canary_telemetry_routes') != canary_routes(telemetry[:2]):
        raise IntegrationIntegrityError('canary telemetry routes do not match prefix')
    count, size, digest = (state_entry.get(k, state_entry['first_telemetry_count' if k == 'attempt_telemetry_count' else 'telemetry_prefix_size' if k == 'attempt_telemetry_prefix_size' else 'telemetry_prefix_sha256']) for k in ('attempt_telemetry_count', 'attempt_telemetry_prefix_size', 'attempt_telemetry_prefix_sha256'))
    if not isinstance(count, int) or not isinstance(size, int) or (not isinstance(digest, str)):
        raise IntegrationError('resume telemetry boundary is incomplete')
    authenticated_telemetry_prefix(telemetry_path, count=count, size=size, digest=digest)
    return (events, telemetry)

def telemetry_evidence(path: Path, *, candidate: Candidate, candidate_spec: CandidateSpec, start_index: int=0) -> dict[str, Any]:
    empty = {'reasoning_tokens': 0, 'model_mismatch_count': 0, 'unknown_required_usage_count': 1, 'logical_call_count': 0, 'attempt_count': 0, 'operation_count': 0, 'agent_count': 0, 'retry_count': 0, 'translate_call_count': 0, 'valid': False}
    if not path.is_file():
        return empty
    try:
        records = [CallAttemptTelemetry.model_validate(json.loads(line)) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()][start_index:]
    except Exception:
        return empty
    expected = {'translator': model_identity(candidate.translator_model), 'analyst': model_identity(candidate.analyst_model), 'editor': model_identity(candidate.editor_model), 'preparer': model_identity(candidate.fast_model)}
    operations = {'translator': {'translate.batch', 'translate.single', 'integration.canary.translate'}, 'analyst': {'analyzer.analyze', 'chapter.classify', 'prescan.name_terms', 'glossary.audit', 'title.translate', 'translate.heading', 'translate.single'}, 'preparer': {'language.detect', 'prescan.term_mine', 'glossary.extract'}, 'editor': {'polish.segment', 'polish.batch', 'translate.repair', 'integration.canary.polish'}}
    mismatch = unknown = reasoning = 0
    for record in records:
        model = expected.get(record.agent)
        if model is None:
            mismatch += 1
            continue
        provider, name, enabled = model
        mismatch += int(record.operation not in operations.get(record.agent, set()) or record.provider != provider or normalized_model(record.requested_model) != name or (normalized_model(record.resolved_model) != name) or (record.reasoning_enabled != enabled) or (record.status != 'success') or (record.temperature != candidate_spec.temperature) or (record.seed is not None))
        unknown += int(record.billed_usage_unknown)
        if not enabled:
            reasoning += record.reasoning_tokens
    return {'reasoning_tokens': reasoning, 'model_mismatch_count': mismatch, 'unknown_required_usage_count': unknown, 'logical_call_count': len({r.logical_call_id for r in records}), 'attempt_count': len(records), 'operation_count': len({r.operation for r in records}), 'agent_count': len({r.agent for r in records}), 'retry_count': sum(r.attempt_index > 1 or r.retry_class is not None for r in records), 'translate_call_count': translator_call_count(records), 'valid': True}

def failure_code(error: BaseException) -> str:
    if 'publication proof' in str(error).casefold():
        return 'publication_proof'
    for fragment, code in (('resume event boundary', 'resume_event_boundary'), ('resume telemetry boundary', 'resume_telemetry_boundary'), ('resume reuse proof', 'resume_reuse_proof'), ('resume translation call attribution', 'resume_telemetry_attribution'), ('usage and application telemetry', 'usage_telemetry_mismatch'), ('crashed resume evidence', 'crashed_resume_evidence'), ('event slice mismatch', 'resume_event_slice'), ('telemetry prefix', 'telemetry_prefix'), ('event prefix', 'event_prefix'), ('canary', 'canary_evidence'), ('phase timing', 'phase_timing'), ('bilingual output', 'bilingual_output'), ('readiness', 'readiness'), ('first telemetry', 'first_telemetry_evidence')):
        if fragment in str(error).casefold():
            return code
    return 'integration_error'

def timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return round(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        return None

def recovered_active_duration_ms(started_at: Any, *, events: list[dict[str, Any]], telemetry: list[CallAttemptTelemetry]) -> int:
    start = timestamp_ms(started_at)
    if start is None:
        return 0
    ends = [timestamp_ms(r.get(k)) for r in events for k in ('ts', 'at', 'timestamp', 'created_at') if timestamp_ms(r.get(k)) is not None]
    ends += [timestamp_ms(r.started_at) + max(0, int(r.elapsed_ms)) for r in telemetry if timestamp_ms(r.started_at) is not None]
    return max(0, max(ends) - start) if ends else 0

def node_phase_timings(store: RunStore) -> dict[str, int]:
    state, totals, seen = (store.load_state(), {'prepare': 0, 'translate': 0, 'quality': 0}, {'prepare': False, 'translate': False, 'quality': False})
    phases = {'prepare': {'prepare', 'analyze', 'mine_terms', 'name_terms'}, 'translate': {'translate', 'titles'}, 'quality': {'polish', 'deterministic_qa', 'repair', 'report', 'assemble'}}
    phase_by_node = {node: phase for phase, nodes in phases.items() for node in nodes}
    for node_id, node in state.nodes.items():
        if not node.started_at or not node.finished_at:
            continue
        phase = phase_by_node.get(node_id.split(':', 1)[0].casefold())
        if phase is None:
            raise IntegrationError(f'unknown production node timing: {node_id}')
        try:
            elapsed = (datetime.fromisoformat(node.finished_at.replace('Z', '+00:00')) - datetime.fromisoformat(node.started_at.replace('Z', '+00:00'))).total_seconds() * 1000
        except ValueError as error:
            raise IntegrationError(f'invalid production node timing: {node_id}') from error
        if elapsed < 0:
            raise IntegrationError(f'production node timing is negative: {node_id}')
        totals[phase] += round(elapsed)
        seen[phase] = True
    if not all(seen.values()):
        raise IntegrationError('required production phase timing evidence is missing')
    return totals

def _paths(ctx: dict[str, Any]) -> None:
    out, source, spec, cid = (ctx['out'], ctx['source'], ctx['spec'], ctx['cid'])
    root = out / 'candidates' / cid
    ctx.update(root=root, state_dir=root / 'state', output_dir=root / 'outputs', telemetry_path=root / 'telemetry.jsonl', canary_path=root / 'canary.json', result_path=root / 'result.json', first_telemetry_path=root / 'telemetry.first.jsonl', resume_telemetry_path=root / 'telemetry.resume.jsonl')
    validate_candidate_paths(out, source, root, ctx['state_dir'], ctx['output_dir'], ctx['output_dir'] / f'{spec.book_id}.epub', ctx['output_dir'] / f'{spec.book_id}-bi.epub', ctx['telemetry_path'], ctx['first_telemetry_path'], ctx['resume_telemetry_path'], ctx['canary_path'], ctx['result_path'])
    ctx['output_dir'].mkdir(parents=True, exist_ok=True)

def _base_result(ctx: dict[str, Any]) -> dict[str, Any]:
    spec, source_hash, cid = (ctx['spec'], ctx['source_hash'], ctx['cid'])
    request = read_integration_canonical(ctx['out'] / 'integration_request.json')
    result = {'schema_version': 1, 'candidate_id': cid, 'passed': False, 'canary_passed': False, 'expected_interruption_observed': False, 'resume_duplicate_operations': 0, 'readiness_passed': False, 'readiness_problem_count': 0, 'readiness_codes': [], 'structural': {'structural_pass': False}, 'mono': {'structural_pass': False}, 'bilingual': {'structural_pass': False}, 'reasoning_tokens': 0, 'model_mismatch_count': 0, 'unknown_required_usage_count': 0, 'source_sha256': source_hash, 'benchmark_id': spec.benchmark_id, 'book_id': spec.book_id, 'interruption': {'count': 0, 'batches': []}, 'request_sha256': integration_sha256(ctx['out'] / 'integration_request.json')}
    result.update({k: request[k] for k in ('corpus_sha256', 'book_spec_sha256', 'candidate_spec_sha256', 'integration_spec_sha256')})
    return result

def _interrupt(ctx: dict[str, Any], sink: JsonlCallTelemetrySink, hook: Any, store: RunStore, started: float, canary_count: int) -> None:
    events, persisted = (event_rows(store), batch_rows(event_rows(store)))
    if not hook.reached or not hook.raised or (not persisted) or (hook.committed[-1] != {'chapter': persisted[-1]['chapter'], 'start': persisted[-1]['start'], 'count': persisted[-1]['count']}):
        raise IntegrationError('unverified benchmark interruption')
    event_raw, telemetry_raw = (Path(store.event_log_path).read_bytes(), ctx['telemetry_path'].read_bytes())
    ctx['first_telemetry_path'].write_bytes(telemetry_raw)
    entry = {'status': 'interrupted', 'state_path': integration_relative_path(ctx['state_dir'], ctx['out']), 'canary_sha256': integration_sha256(ctx['canary_path']), 'interruption': {'count': len(hook.committed), 'batches': hook.committed}, 'canary_telemetry_routes': canary_routes([CallAttemptTelemetry.model_validate(v) for v in sink.records[:canary_count]]), 'boundary_event_count': len(events), 'first_boundary_event_count': len(events), 'attempt_event_boundary_count': len(events), 'event_prefix_size': len(event_raw), 'event_prefix_sha256': hashlib.sha256(event_raw).hexdigest(), 'first_event_prefix_size': len(event_raw), 'first_event_prefix_sha256': hashlib.sha256(event_raw).hexdigest(), 'before_target_hashes': persisted, 'first_telemetry_path': integration_relative_path(ctx['first_telemetry_path'], ctx['out']), 'first_telemetry_sha256': integration_sha256(ctx['first_telemetry_path']), 'canary_telemetry_count': canary_count, 'first_telemetry_count': len(sink.records), 'telemetry_prefix_size': len(telemetry_raw), 'telemetry_prefix_sha256': hashlib.sha256(telemetry_raw).hexdigest(), 'first_telemetry_prefix_size': len(telemetry_raw), 'first_telemetry_prefix_sha256': hashlib.sha256(telemetry_raw).hexdigest(), 'attempt_telemetry_count': len(sink.records), 'attempt_telemetry_prefix_size': len(telemetry_raw), 'attempt_telemetry_prefix_sha256': hashlib.sha256(telemetry_raw).hexdigest(), 'resume_wall_ms': 0, 'resume_durations_ms': [], 'first_wall_ms': int((time.monotonic() - started) * 1000)}
    ctx['state_entry'] = entry
    ctx['state']['candidates'][ctx['cid']] = entry
    ctx['result']['expected_interruption_observed'] = True
    ctx['result']['interruption'] = entry['interruption']
    write_integration_json(ctx['state_path'], ctx['state'])

def _pending(ctx: dict[str, Any]) -> None:
    sink, options = (JsonlCallTelemetrySink(ctx['telemetry_path']), GenerationOptions(**GENERATION_FIELDS))
    config = quality_config(ctx['candidate_spec'], ctx['candidate'], ctx['state_dir'])
    config.output.bilingual.order = ctx['spec'].bilingual_order
    mono = ctx['output_dir'] / f"{ctx['spec'].book_id}.epub"
    if mono.resolve() == ctx['source'].resolve():
        raise IntegrationError('mono output aliases source EPUB')
    canary = run_canary(ctx['client_provider'](ctx['candidate_spec'], ctx['candidate'], options, sink), ctx['candidate'], ctx['candidate_spec'], sink)
    count = len(sink.records)
    write_integration_json(ctx['canary_path'], canary)
    ctx['result'].update(canary_passed=bool(canary.get('passed')), reasoning_tokens=int(canary.get('reasoning_tokens', 0)), model_mismatch_count=int(canary.get('model_mismatch_count', 0)), unknown_required_usage_count=int(canary.get('unknown_required_usage_count', 0)))
    if not ctx['result']['canary_passed']:
        raise IntegrationError('synthetic canary failed')
    hook = ctx['hook_factory'](ctx['spec'].interrupt_after_committed_batches)
    ctx['state']['candidates'][ctx['cid']] = {'status': 'pending', 'state_path': integration_relative_path(ctx['state_dir'], ctx['out'])}
    write_integration_json(ctx['state_path'], ctx['state'])
    try:
        Application(config, client=ctx['client_provider'](ctx['candidate_spec'], ctx['candidate'], options, sink), batch_commit_hook=hook).run_all(str(ctx['source']), out_format='epub', out_path=str(mono))
    except ctx['interruption_type']:
        _interrupt(ctx, sink, hook, candidate_store(ctx['state_dir']), ctx['started'], count)
    else:
        raise IntegrationError('configured interruption was not observed')

def _existing(ctx: dict[str, Any]) -> None:
    entry, path = (ctx['state']['candidates'][ctx['cid']], ctx['canary_path'])
    if not path.exists():
        raise IntegrationError('resuming candidate is missing canary evidence')
    canary = read_integration_canonical(path)
    if entry.get('canary_sha256') != integration_sha256(path):
        raise IntegrationIntegrityError('resuming candidate canary hash mismatch')
    store = candidate_store(ctx['state_dir'])
    if ctx['candidate_status'] in {'interrupted', 'resuming'}:
        validate_restart_prefixes(entry, candidate_store=store, telemetry_path=ctx['telemetry_path'])
        ctx['result'].update(expected_interruption_observed=True, interruption=entry['interruption'])
    events = event_rows(store)
    ctx['result'].update(canary_passed=bool(canary.get('passed')), reasoning_tokens=int(canary.get('reasoning_tokens', 0)), model_mismatch_count=int(canary.get('model_mismatch_count', 0)), unknown_required_usage_count=int(canary.get('unknown_required_usage_count', 0)))
    ctx.update(state_entry=entry, store=store, events=events, boundary_event_count=int(entry.get('attempt_event_boundary_count', entry.get('boundary_event_count', 0))), attempt_telemetry_count=int(entry.get('attempt_telemetry_count', entry.get('first_telemetry_count', 0))), canary_count=int(entry['canary_telemetry_count']))
    if len(events) < ctx['boundary_event_count']:
        raise IntegrationError('resume event boundary is beyond persisted events')

def _recover(ctx: dict[str, Any]) -> None:
    if ctx['candidate_status'] != 'resuming':
        return
    entry, events = (ctx['state_entry'], ctx['events'])
    before = list(entry['before_target_hashes'])
    prior = {(r['chapter'], r['start'], r['count']): r['target_sha256'] for r in before}
    new = batch_rows(events[ctx['boundary_event_count']:])
    for row in new:
        ident = (row['chapter'], row['start'], row['count'])
        if ident in prior:
            if prior[ident] != row['target_sha256']:
                raise IntegrationIntegrityError('resume committed target hash changed')
            raise IntegrationIntegrityError('resume retranslated committed batch')
    old_boundary, old_attempt, old_size, old_tsize = (ctx['boundary_event_count'], ctx['attempt_telemetry_count'], int(entry.get('event_prefix_size', 0)), int(entry.get('attempt_telemetry_prefix_size', entry.get('telemetry_prefix_size', 0))))
    before.extend(new)
    records = read_telemetry_records(ctx['telemetry_path'])
    if len(records) < old_attempt:
        raise IntegrationError('resume telemetry boundary is beyond persisted attempts')
    event_raw, telemetry_raw = (Path(ctx['store'].event_log_path).read_bytes(), ctx['telemetry_path'].read_bytes())
    recovered_events, recovered_telemetry = (parse_event_bytes(event_raw[old_size:]), [CallAttemptTelemetry.model_validate(json.loads(line)) for line in telemetry_raw[old_tsize:].decode().splitlines() if line.strip()])
    if batch_rows(recovered_events) != new:
        raise IntegrationIntegrityError('crashed resume event slice mismatch')
    duration = recovered_active_duration_ms(entry.get('resume_started_at'), events=recovered_events, telemetry=recovered_telemetry)
    durations = list(entry.get('resume_durations_ms', []))
    if duration:
        durations.append(duration)
    entry.update({'before_target_hashes': before, 'boundary_event_count': len(event_rows(ctx['store'])), 'attempt_event_boundary_count': len(event_rows(ctx['store'])), 'event_prefix_size': len(event_raw), 'event_prefix_sha256': hashlib.sha256(event_raw).hexdigest(), 'attempt_telemetry_count': len(records), 'attempt_telemetry_prefix_size': len(telemetry_raw), 'attempt_telemetry_prefix_sha256': hashlib.sha256(telemetry_raw).hexdigest(), 'telemetry_prefix_size': len(telemetry_raw), 'telemetry_prefix_sha256': hashlib.sha256(telemetry_raw).hexdigest(), 'resume_wall_ms': int(entry.get('resume_wall_ms', 0)) + duration, 'resume_durations_ms': durations, 'recovered_resume': {'event_boundary_count': old_boundary, 'event_count': len(recovered_events), 'event_sha256': hashlib.sha256(event_raw[old_size:]).hexdigest(), 'telemetry_boundary_count': old_attempt, 'telemetry_count': len(recovered_telemetry), 'telemetry_sha256': hashlib.sha256(telemetry_raw[old_tsize:]).hexdigest(), 'active_duration_ms': duration}})
    ctx.update(state_entry=entry, events=event_rows(ctx['store']), boundary_event_count=len(event_rows(ctx['store'])), attempt_telemetry_count=len(records))
    write_integration_json(ctx['state_path'], {**ctx['state'], 'candidates': {**ctx['state']['candidates'], ctx['cid']: entry}})

def _resume(ctx: dict[str, Any]) -> dict[str, Any]:
    spec, entry, store = (ctx['spec'], ctx['state_entry'], ctx['store'])
    before = list(entry.get('before_target_hashes', []))
    before_ids = {(r['chapter'], r['start'], r['count']): r['target_sha256'] for r in before}
    ctx['result']['before_target_hashes'] = before
    sink = JsonlCallTelemetrySink(ctx['telemetry_path'])
    first_count = int(entry['first_telemetry_count'])
    attempt_count = int(entry.get('attempt_telemetry_count', first_count))
    started = time.monotonic()
    options = GenerationOptions(**GENERATION_FIELDS)
    client = ctx['client_provider'](ctx['candidate_spec'], ctx['candidate'], options, sink)
    entry.update({'status': 'resuming', 'state_path': integration_relative_path(ctx['state_dir'], ctx['out']), 'resume_attempt': int(entry.get('resume_attempt', 0)) + 1, 'resume_started_wall_ms': int((time.monotonic() - ctx['started']) * 1000), 'resume_started_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')})
    ctx['state']['candidates'][ctx['cid']] = entry
    write_integration_json(ctx['state_path'], ctx['state'])
    mono = ctx['output_dir'] / f'{spec.book_id}.epub'
    config = quality_config(ctx['candidate_spec'], ctx['candidate'], ctx['state_dir'])
    config.output.bilingual.order = spec.bilingual_order
    result_value = Application(config, client=client).run_all(str(ctx['source']), out_format='epub', out_path=str(mono))
    raw = ctx['telemetry_path'].read_bytes()
    first_size = int(entry.get('first_telemetry_prefix_size', entry['telemetry_prefix_size']))
    current_size = int(entry.get('attempt_telemetry_prefix_size', entry['telemetry_prefix_size']))
    resume_raw, current_raw = (raw[first_size:], raw[current_size:])
    ctx['resume_telemetry_path'].write_bytes(resume_raw)
    entry['resume_telemetry_count'] = len(resume_raw.splitlines())
    post = event_rows(store)[ctx['boundary_event_count']:]
    all_batches = batch_rows(event_rows(store))
    post_batches = batch_rows(post)
    skips = skip_rows(post)
    remaining = len(post_batches)
    required = Counter((r['chapter'], r['start'], r['count'], r['target_sha256']) for r in before)
    found = Counter((r['chapter'], r['start'], r['count'], r['target_sha256']) for r in skips)
    complete = not skips and remaining == 0 and all(any(x['chapter'] == r['chapter'] and x['start'] == r['start'] and (x['count'] == r['count']) and (x['target_sha256'] == r['target_sha256']) for x in all_batches) for r in before)
    if not complete and (any(k not in required for k in found) or any((found[k] < n for k, n in required.items()))):
        raise IntegrationError('resume reuse proof is incomplete or mismatched')
    repeated = [r for r in post_batches if (r['chapter'], r['start'], r['count']) in before_ids]
    final = {(r['chapter'], r['start'], r['count']): r['target_sha256'] for r in all_batches}
    changed = [i for i, h in before_ids.items() if final.get(i) != h]
    ctx.update(result_value=result_value, raw=raw, resume_raw=resume_raw, current_raw=current_raw, current_resume_telemetry=telemetry_evidence(ctx['telemetry_path'], candidate=ctx['candidate'], candidate_spec=ctx['candidate_spec'], start_index=attempt_count), all_batches=all_batches, post=post, before=before, resumed_elapsed=max(int((time.monotonic() - started) * 1000), 0), duplicate=len(repeated) + len(changed), repeated_count=len(repeated), skips=skips, remaining=remaining)
    return ctx['result']

def _finish(ctx: dict[str, Any]) -> None:
    result, spec, store = (ctx['result'], ctx['spec'], ctx['store'])
    structural = validate_epub_triplet(
        ctx['source'],
        ctx['output_dir'] / f'{spec.book_id}.epub',
        ctx['output_dir'] / f'{spec.book_id}-bi.epub',
        publication_report=store.load_epub_verification(),
        output_digest=ctx['result_value'].get('output_digest'),
    )
    telemetry = telemetry_evidence(ctx['telemetry_path'], candidate=ctx['candidate'], candidate_spec=ctx['candidate_spec'])
    usage = usage_evidence(Path(store.usage_path))
    app_attempts = max(0, telemetry['attempt_count'] - ctx['canary_count'])
    if usage.get('valid') and usage['attempts'] != app_attempts:
        raise IntegrationError('RunStore usage and application telemetry mismatch')
    readiness_problems = assemble_readiness_problems(store)
    result.update(resume_duplicate_operations=ctx['duplicate'], committed_batches=len(ctx['before']), skipped_batches=len(ctx['skips']), remaining_batches=ctx['remaining'], repeated_batches=ctx['repeated_count'], final_target_hashes=ctx['all_batches'], readiness_problem_count=len(readiness_problems), readiness_codes=sorted({hashlib.sha256(problem.encode('utf-8')).hexdigest()[:16] for problem in readiness_problems}), readiness_passed=not readiness_problems, structural=structural, mono=structural.get('mono', {'structural_pass': False}), bilingual=structural.get('bilingual', {'structural_pass': False}), telemetry_counts={k: telemetry[k] for k in ('logical_call_count', 'attempt_count', 'operation_count', 'agent_count', 'retry_count', 'translate_call_count')}, unknown_required_usage_count=int(telemetry['unknown_required_usage_count']) + int(usage['unknown_required_usage_count']), reasoning_tokens=telemetry['reasoning_tokens'], model_mismatch_count=telemetry['model_mismatch_count'])
    if ctx['state_entry'].get('recovered_resume') is not None:
        result['recovered_resume'] = ctx['state_entry']['recovered_resume']
    if ctx['current_resume_telemetry']['valid'] and ctx['current_resume_telemetry']['translate_call_count'] != sum(r['translate_call_count'] for r in batch_rows(ctx['post'])):
        raise IntegrationError('resume translation call attribution mismatch')
    bilingual = ctx['output_dir'] / f'{spec.book_id}-bi.epub'
    outputs = ctx['result_value'].get('outputs', [])
    if str(bilingual) not in outputs and (not bilingual.exists()):
        raise IntegrationError('bilingual output missing')
    result.update(output_paths={'mono': integration_relative_path(ctx['output_dir'] / f'{spec.book_id}.epub', ctx['out']), 'bilingual': integration_relative_path(bilingual, ctx['out'])}, output_sha256={'mono': integration_sha256(ctx['output_dir'] / f'{spec.book_id}.epub'), 'bilingual': integration_sha256(bilingual)})
    if not ctx['telemetry_path'].exists():
        raise IntegrationError('telemetry artifact missing')
    result.update(telemetry_path=integration_relative_path(ctx['telemetry_path'], ctx['out']), telemetry_sha256=integration_sha256(ctx['telemetry_path']), usage_path=integration_relative_path(Path(store.usage_path), ctx['out']), usage_sha256=integration_sha256(Path(store.usage_path)), resume_telemetry_path=integration_relative_path(ctx['resume_telemetry_path'], ctx['out']), resume_telemetry_sha256=integration_sha256(ctx['resume_telemetry_path']), resume_telemetry_count=len(ctx['resume_raw'].splitlines()), resume_attempt_telemetry_count=len(ctx['current_raw'].splitlines()))
    first_relative = ctx['state_entry'].get('first_telemetry_path')
    first_hash = ctx['state_entry'].get('first_telemetry_sha256')
    first_count = ctx['state_entry'].get('first_telemetry_count')
    first = (ctx['out'] / first_relative).resolve() if isinstance(first_relative, str) else ctx['out']
    try:
        first_contained = integration_relative_path(first, ctx['out']) == first_relative
    except IntegrationError:
        first_contained = False
    if not first_contained or not isinstance(first_hash, str) or type(first_count) is not int or not first.is_file() or integration_sha256(first) != first_hash or len(read_telemetry_records(first)) != first_count:
        raise IntegrationError('first telemetry evidence is missing or tampered')
    result.update(first_telemetry_path=ctx['state_entry'].get('first_telemetry_path'), first_telemetry_sha256=first_hash, first_telemetry_count=first_count)
    first_ms, resume_ms = (int(ctx['state_entry'].get('first_wall_ms', 0)), int(ctx['state_entry'].get('resume_wall_ms', 0)) + ctx['resumed_elapsed'])
    ctx['state_entry']['resume_wall_ms'] = resume_ms
    ctx['state_entry']['resume_durations_ms'] = [*ctx['state_entry'].get('resume_durations_ms', []), ctx['resumed_elapsed']]
    total = first_ms + resume_ms
    result['phase_timings_ms'] = {**node_phase_timings(store), 'first_attempt': first_ms, 'resume': resume_ms, 'total': total}
    result['passed'] = bool(result['canary_passed'] and result['expected_interruption_observed'] and result['readiness_passed'] and (result['resume_duplicate_operations'] == 0) and (structural.get('structural_pass') is True) and (result['mono'].get('structural_pass') is True) and (result['bilingual'].get('structural_pass') is True) and (result['unknown_required_usage_count'] == 0) and (result['reasoning_tokens'] == 0) and (result['model_mismatch_count'] == 0))
    result['wall_time_seconds'] = total / 1000
    result['total_wall_ms'] = total
    request_path = ctx['out'] / 'integration_request.json'
    request = read_integration_canonical(request_path)
    request_hash = integration_sha256(request_path)
    lineage_keys = ('corpus_sha256', 'book_spec_sha256', 'candidate_spec_sha256', 'integration_spec_sha256')
    if result.get('request_sha256') != request_hash or any(result.get(key) != request.get(key) for key in lineage_keys):
        raise IntegrationIntegrityError('integration request lineage changed during execution')
    result.update(request_sha256=request_hash, **{key: request[key] for key in lineage_keys})

def run_candidate(client_provider: Any, hook_factory: Any, interruption_type: type[BaseException], spec: Any, candidate_spec: CandidateSpec, candidate: Candidate, source: Path, source_hash: str, out: Path, state: dict[str, Any], state_path: Path) -> dict[str, Any]:
    ctx = {'client_provider': client_provider, 'hook_factory': hook_factory, 'interruption_type': interruption_type, 'spec': spec, 'candidate_spec': candidate_spec, 'candidate': candidate, 'source': source, 'source_hash': source_hash, 'out': out, 'state': state, 'state_path': state_path, 'cid': candidate.candidate_id, 'started': time.monotonic()}
    _paths(ctx)
    ctx['result'] = _base_result(ctx)
    status = state['candidates'][candidate.candidate_id]['status']
    ctx['candidate_status'] = status
    if status not in {'pending', 'interrupted', 'resuming'}:
        raise IntegrationError('candidate is not resumable')
    try:
        if status == 'pending':
            _pending(ctx)
            entry = state['candidates'][candidate.candidate_id]
            store = candidate_store(ctx['state_dir'])
            ctx.update(state_entry=entry, store=store, events=event_rows(store), boundary_event_count=int(entry.get('boundary_event_count', 0)), attempt_telemetry_count=int(entry.get('attempt_telemetry_count', entry.get('first_telemetry_count', 0))), canary_count=int(entry.get('canary_telemetry_count', 2)))
        else:
            _existing(ctx)
            _recover(ctx)
        _resume(ctx)
        _finish(ctx)
        final_status = 'completed' if ctx['result']['passed'] else 'failed'
        state['candidates'][candidate.candidate_id] = {**ctx['state_entry'], 'status': final_status, 'state_path': integration_relative_path(ctx['state_dir'], out), 'result_path': integration_relative_path(ctx['result_path'], out)}
    except IntegrationIntegrityError:
        raise
    except Exception as error:
        result, code = (ctx['result'], failure_code(error))
        reasons = result.setdefault('failure_reasons', [])
        [reasons.append(v) for v in (type(error).__name__, code) if v not in reasons]
        result.update(failure_code=code, unknown_required_usage_count=max(1, int(result.get('unknown_required_usage_count', 0))), wall_time_seconds=time.monotonic() - ctx['started'])
        state['candidates'][candidate.candidate_id] = {**ctx.get('state_entry', state['candidates'][candidate.candidate_id]), 'status': 'failed', 'state_path': integration_relative_path(ctx['state_dir'], out), 'reason': code}
    write_integration_json(ctx['result_path'], ctx['result'])
    state['candidates'][candidate.candidate_id]['result_sha256'] = integration_sha256(ctx['result_path'])
    write_integration_json(state_path, state)
    return ctx['result']
__all__ = ['TRANSLATOR_OPERATIONS', 'authenticated_event_prefix', 'authenticated_telemetry_prefix', 'batch_rows', 'candidate_store', 'event_rows', 'failure_code', 'node_phase_timings', 'parse_event_bytes', 'recovered_active_duration_ms', 'run_candidate', 'skip_rows', 'telemetry_evidence', 'timestamp_ms', 'validate_restart_prefixes']

# fmt: on
