# fmt: off

"""Benchmark integration observable contracts."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from typer.testing import CliRunner

from tests.fixtures.books import write_sample_epub
from tests.fixtures.fake_llm import routing_handler
from trans_novel.benchmark.artifacts import canonical_json, sha256_bytes
from trans_novel.benchmark.corpus.identity import passage_id, segment_id
from trans_novel.benchmark.integration import IntegrationRunner, IntegrationSpec
from trans_novel.benchmark.integration.artifacts import IntegrationError, translator_call_count
from trans_novel.benchmark.integration.preflight import preflight
from trans_novel.benchmark.integration.resume import telemetry_evidence
from trans_novel.benchmark.schema import Candidate, CandidateSpec
from trans_novel.cli import app
from trans_novel.llm import FakeClient
from trans_novel.llm.telemetry import CallAttemptTelemetry
from trans_novel.model_profiles import parse_model_selection, parse_provider_model
from trans_novel.pipeline.state import RUN_INPUT_SCHEMA_VERSION


def _spec(**updates):
    value = {'schema_version': 1, 'benchmark_id': 'phase9', 'corpus_sha256': 'a' * 64, 'candidate_spec_sha256': 'b' * 64, 'book_id': 'hidden-book', 'candidate_ids': ['candidate-a', 'candidate-b'], 'interrupt_after_committed_batches': 1, 'output_mono': True, 'output_bilingual': True, 'bilingual_order': 'target_first', 'source_language': 'en', 'target_language': 'zh'}
    value.update(updates)
    return value

def _telemetry_record(operation: str, *, agent: str='translator', index: int=1):
    return CallAttemptTelemetry(schema_version=1, logical_call_id=f'{index:032x}', attempt_index=1, started_at='2026-01-01T00:00:00.000Z', elapsed_ms=0, stage='translate', agent=agent, operation=operation, provider='fake', requested_model='model', resolved_model='model', reasoning_enabled=False, reasoning_effort=None, temperature=0.1, seed=None, json_mode=False, max_tokens=None, status='success', retry_class=None, http_status=None, finish_reason=None, response_id=None, prompt_tokens=0, completion_tokens=0, total_tokens=0, cache_hit_tokens=0, cache_miss_tokens=0, reasoning_tokens=0, billed_usage_unknown=False, request_sha256='a' * 64, response_sha256='b' * 64)

class _InstrumentedFakeClient(FakeClient):

    def __init__(self, *, handler, models: tuple[str, str, str, str], provider: str='fake') -> None:
        super().__init__(handler=handler)
        self.models, self.provider, self.telemetry_sink, self._attempts = (models, provider, None, 0)

    def set_telemetry_sink(self, sink) -> None:
        self.telemetry_sink = sink

    def complete(self, messages, *, json_mode=False, max_tokens=None, stage=None, agent, operation):
        response = super().complete(messages, json_mode=json_mode, max_tokens=max_tokens, stage=stage, agent=agent, operation=operation)
        self._attempts += 1
        model_ref = self.models[1] if agent == 'analyst' else self.models[2] if agent == 'editor' else self.models[3] if agent == 'preparer' else self.models[0]
        provider, model = parse_provider_model(model_ref)
        selection = parse_model_selection(model)
        started = '2026-01-01T00:00:00.000Z'
        self.telemetry_sink.record(CallAttemptTelemetry(schema_version=1, logical_call_id=f'{self._attempts:032x}', attempt_index=1, started_at=started, elapsed_ms=0, stage=stage, agent=agent, operation=operation, provider=provider, requested_model=selection.model, resolved_model=selection.model, reasoning_enabled=False, reasoning_effort=None, temperature=0.1, seed=None, json_mode=json_mode, max_tokens=max_tokens, status='success', retry_class=None, http_status=None, finish_reason=None, response_id=None, prompt_tokens=0, completion_tokens=0, total_tokens=0, cache_hit_tokens=0, cache_miss_tokens=0, reasoning_tokens=0, billed_usage_unknown=False, request_sha256=hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), response_sha256=hashlib.sha256(response.encode()).hexdigest()))
        return response

def _artifact_hash(corpus: dict, runner: list[dict], challenge_keys: list[dict]) -> str:
    semantics = {'corpus': {key: value for key, value in corpus.items() if key != 'corpus_sha256'}, 'runner_segments': runner, 'challenge_keys': challenge_keys}
    encoded = json.dumps(semantics, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

def _write_minimal_artifact(root: Path, runner: list[dict], *, manifest_books: list[dict] | None=None, challenge_keys: list[dict] | None=None) -> None:
    challenge_keys = challenge_keys or []
    manifest_books = manifest_books or [{'book_id': 'book', 'source_sha256': 'a' * 64, 'basename': 'book.txt', 'split': 'screen', 'format': 'text', 'title': 'book', 'chapter_count': 1, 'parser_schema': RUN_INPUT_SCHEMA_VERSION}]
    corpus = {'schema_version': 1, 'benchmark_name': 'fixture', 'word_counter': 'en-v1', 'parser_schema': RUN_INPUT_SCHEMA_VERSION, 'run_input_schema_version': RUN_INPUT_SCHEMA_VERSION, 'books': manifest_books, 'passages': [{key: row[key] for key in ('passage_id', 'subset', 'book_id', 'chapter_index', 'start', 'end', 'word_count', 'strata')} for row in runner], 'quotas': {'targets': {'screen': 10000, 'continuous': 30000, 'stratified': 15000, 'context': 5000}, 'actual': {'screen': 0, 'continuous': 0, 'stratified': 0, 'context': 0, 'hidden': 0, 'formal': 0}, 'tolerance': 0.2}}
    corpus['corpus_sha256'] = _artifact_hash(corpus, runner, challenge_keys)
    (root / 'corpus.json').write_text(json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    (root / 'source_manifest.json').write_text(json.dumps({'schema_version': 1, 'run_input_schema_version': RUN_INPUT_SCHEMA_VERSION, 'books': manifest_books}, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    (root / 'runner_segments.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n' for row in runner), encoding='utf-8')
    (root / 'challenge_keys.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n' for row in challenge_keys), encoding='utf-8')

def _finish_real_corpus(corpus: Path, rows: list[dict], keys: list[dict]) -> None:
    value = json.loads((corpus / 'corpus.json').read_text())
    value['quotas']['actual'] = {'screen': 10000, 'continuous': 30000, 'stratified': 15000, 'context': 5000, 'hidden': 0, 'formal': 50000}
    value['corpus_sha256'] = _artifact_hash(value, rows, keys)
    (corpus / 'corpus.json').write_text(canonical_json(value) + '\n', encoding='utf-8')

def _real_preflight_corpus(root: Path) -> tuple[Path, list[dict], list[dict], list[dict]]:
    corpus = root / 'corpus'
    corpus.mkdir()
    hidden = root / 'hidden.epub'
    write_sample_epub(str(hidden))
    hidden_hash = sha256_bytes(hidden.read_bytes())
    manifests, books = ([], [])
    for split, count in (('screen', 3), ('formal', 6)):
        for index in range(count):
            book_id = f'{split}-{index}'
            path = root / f'{book_id}.txt'
            path.write_text(f'{book_id} fixture', encoding='utf-8')
            digest = sha256_bytes(path.read_bytes())
            manifests.append({'book_id': book_id, 'source_sha256': digest, 'basename': path.name, 'split': split, 'format': 'text', 'title': book_id, 'chapter_count': 1, 'parser_schema': RUN_INPUT_SCHEMA_VERSION})
            books.append({'book_id': book_id, 'path': path.name, 'split': split})
    manifests.append({'book_id': 'hidden-book', 'source_sha256': hidden_hash, 'basename': hidden.name, 'split': 'hidden', 'format': 'epub', 'title': 'hidden-book', 'chapter_count': 2, 'parser_schema': RUN_INPUT_SCHEMA_VERSION})
    books.append({'book_id': 'hidden-book', 'path': hidden.name, 'split': 'hidden'})
    hashes = {row['book_id']: row['source_sha256'] for row in manifests}
    indexes = dict.fromkeys(hashes, 0)
    rows, keys = ([], [])

    def add(book_id: str, subset: str, words: int, strata: list[str] | None=None, *, context: bool=False) -> None:
        index = indexes[book_id] if not context else 3000 + sum(row['subset'] == 'context' for row in rows)
        text = 'word ' * words
        segment = {'segment_id': segment_id(hashes[book_id], 0, index, text), 'index': index, 'source': text, 'kind': 'text', 'cont': False, 'anchor': None, 'resource_href': None, 'meta': {}}
        row = {'passage_id': passage_id(book_id, 0, index, index, [text]), 'subset': subset, 'book_id': book_id, 'chapter_index': 0, 'start': index, 'end': index, 'word_count': words, 'strata': strata or [], 'segments': [segment], 'context': None}
        if context:
            before = 2000 + sum(item['subset'] == 'context' for item in rows) - 1
            before_id = segment_id(hashes[book_id], 0, before, 'context before')
            row['context'] = {'challenge_type': 'chapter_transition', 'source_before': [{'segment_id': before_id, 'source': 'context before'}], 'source_after': [], 'frozen_target_before': [{'segment_id': before_id, 'target': 'context target'}]}
            keys.append({'passage_id': row['passage_id'], 'challenge_type': 'chapter_transition', 'answer_key': 'fixture-answer', 'rationale': 'fixture-rationale'})
        rows.append(row)
        indexes[book_id] = max(indexes[book_id], index + 1)
    for book_id, words in zip(('screen-0', 'screen-1', 'screen-2'), (3333, 3333, 3334), strict=True):
        add(book_id, 'screen', words)
    for book_id in ('formal-0', 'formal-1', 'formal-2'):
        add(book_id, 'continuous', 10000)
        for stratum in ('narrative', 'dialogue', 'literary', 'long_sentence', 'idiom_metaphor_wordplay', 'terminology', 'numbers_entities', 'special_format'):
            add(book_id, 'stratified', 312, [stratum])
            add(book_id, 'stratified', 313, [stratum])
    for book_id, words in zip(('formal-3', 'formal-4', 'formal-5'), ((333, 333, 334, 333, 333), (333, 333, 334, 333, 334), (333, 333, 334, 333, 334)), strict=True):
        for count in words:
            add(book_id, 'context', count, context=True)
    _write_minimal_artifact(corpus, rows, manifest_books=manifests, challenge_keys=keys)
    _finish_real_corpus(corpus, rows, keys)
    return (hidden, manifests, books, keys)

def _real_preflight_specs(root: Path, corpus_hash: str, books: list[dict]) -> tuple[Path, Path, Path]:
    candidate_path = root / 'candidates.yaml'
    candidate_path.write_text(yaml.safe_dump({'schema_version': 3, 'benchmark_id': 'phase9', 'temperature': 0.1, 'seed': None, 'replicates': 1, 'candidates': [{'candidate_id': 'a-polished', 'translator_model': 'bailian/qwen3.8-max:off', 'analyst_model': 'bailian/qwen3.7-flash:off', 'editor_model': 'bailian/deepseek-v4-pro:off', 'fast_model': 'bailian/qwen3.7-flash:off', 'pipeline_variant': 'polish'}, {'candidate_id': 'b-polished', 'translator_model': 'bailian/deepseek-v4-flash:off', 'analyst_model': 'bailian/qwen3.7-flash:off', 'editor_model': 'bailian/qwen3.7-plus:off', 'fast_model': 'bailian/qwen3.7-flash:off', 'pipeline_variant': 'polish'}]}, sort_keys=False), encoding='utf-8')
    book_spec = root / 'books.yaml'
    book_spec.write_text(yaml.safe_dump({'schema_version': 1, 'source_language': 'en', 'target_language': 'zh', 'books': books}, sort_keys=False), encoding='utf-8')
    integration = root / 'integration.yaml'
    integration.write_text(yaml.safe_dump(_spec(corpus_sha256=corpus_hash, candidate_spec_sha256=sha256_bytes(candidate_path.read_bytes()), candidate_ids=['a-polished', 'b-polished'], book_id='hidden-book'), sort_keys=False), encoding='utf-8')
    return (candidate_path, book_spec, integration)

class TestBenchmarkIntegrationPreflight(unittest.TestCase):

    def _runner_fixture(self, root: Path, *, interrupt: int=1):
        source = root / 'hidden.epub'
        write_sample_epub(str(source))
        candidate_spec = CandidateSpec.model_validate({'schema_version': 3, 'benchmark_id': 'phase9', 'temperature': 0.1, 'seed': None, 'replicates': 1, 'candidates': [{'candidate_id': 'candidate-a-polished', 'translator_model': 'bailian/qwen3.8-max:off', 'analyst_model': 'bailian/qwen3.7-flash:off', 'editor_model': 'bailian/deepseek-v4-pro:off', 'fast_model': 'bailian/qwen3.7-flash:off', 'pipeline_variant': 'polish'}, {'candidate_id': 'candidate-b-polished', 'translator_model': 'bailian/deepseek-v4-flash:off', 'analyst_model': 'bailian/qwen3.7-flash:off', 'editor_model': 'bailian/qwen3.7-plus:off', 'fast_model': 'bailian/qwen3.7-flash:off', 'pipeline_variant': 'polish'}]})
        selected = list(candidate_spec.candidates)
        integration_spec = IntegrationSpec.model_validate(_spec(candidate_ids=['candidate-a-polished', 'candidate-b-polished'], interrupt_after_committed_batches=interrupt))
        lineage = {'book_spec_sha256': 'd' * 64, 'candidate_spec_sha256': 'b' * 64, 'integration_spec_sha256': 'e' * 64, 'selected': selected, 'source_sha256': sha256_bytes(source.read_bytes())}
        factory_calls: list[FakeClient] = []

        def factory(**kwargs):
            roles = kwargs.get('models')
            if roles is not None:
                models = (roles.translator[0], roles.analyst[0], roles.editor[0], roles.fast[0])
            elif len(factory_calls) < 3:
                models = ('bailian/qwen3.8-max:off', 'bailian/qwen3.7-flash:off', 'bailian/deepseek-v4-pro:off', 'bailian/qwen3.7-flash:off')
            else:
                models = ('bailian/deepseek-v4-flash:off', 'bailian/qwen3.7-flash:off', 'bailian/qwen3.7-plus:off', 'bailian/qwen3.7-flash:off')
            client = _InstrumentedFakeClient(handler=routing_handler, models=models, provider='bailian')
            factory_calls.append(client)
            return client
        runner = IntegrationRunner(client_factory=factory)
        preflight_patch = mock.patch('trans_novel.benchmark.integration.preflight', return_value=(integration_spec, candidate_spec, source, lineage['source_sha256'], lineage))
        preflight_patch.start()
        self.addCleanup(preflight_patch.stop)
        return (runner, source, factory_calls)

    def test_cli_registers_exact_integration_command(self):
        runner = CliRunner()
        result = runner.invoke(app, ['tools', 'benchmark', 'integration', 'run', 'missing', 'book.yaml', 'candidates.yaml', 'integration.yaml', '--out', 'out'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn('No such command', result.output)

    def test_integration_spec_is_strict_and_rejects_controls_or_duplicates(self):
        spec = IntegrationSpec.model_validate(_spec())
        self.assertEqual(spec.schema_version, 1)
        with self.assertRaises(ValueError):
            IntegrationSpec.model_validate(_spec(candidate_ids=['candidate-a', 'candidate-a']))
        with self.assertRaises(ValueError):
            IntegrationSpec.model_validate(_spec(unexpected=True))

    def test_preflight_rejects_source_bilingual_alias_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, source, _clients = self._runner_fixture(root)
            out = root / 'out'
            candidate_root = out / 'candidates' / 'candidate-a-polished'
            (candidate_root / 'outputs').mkdir(parents=True)
            (candidate_root / 'outputs' / 'hidden-book.epub').symlink_to(source)
            with self.assertRaises(IntegrationError):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', out)
            (candidate_root / 'outputs' / 'hidden-book.epub').unlink()
            outside = root / 'outside'
            outside.mkdir()
            (candidate_root / 'state').symlink_to(outside)
            with self.assertRaises(IntegrationError):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', out)
            self.assertTrue(source.exists())

    def test_real_preflight_uses_phase4_manifest_and_strict_spec_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hidden, _manifests, books, _keys = _real_preflight_corpus(root)
            corpus = root / 'corpus'
            corpus_hash = json.loads((corpus / 'corpus.json').read_text())['corpus_sha256']
            candidate_path, book_spec_path, integration_path = _real_preflight_specs(root, corpus_hash, books)
            clients: list[dict] = []
            IntegrationRunner(client_factory=lambda **kwargs: clients.append(kwargs))
            spec, _candidate_spec, source, source_hash, lineage = preflight(corpus, book_spec_path, candidate_path, integration_path, spec_type=IntegrationSpec)
            self.assertEqual(spec.book_id, 'hidden-book')
            self.assertEqual(source, hidden.resolve())
            self.assertEqual(source_hash, sha256_bytes(hidden.read_bytes()))
            self.assertEqual([item.candidate_id for item in lineage['selected']], ['a-polished', 'b-polished'])
            self.assertEqual(clients, [])

class TestBenchmarkIntegrationPreflightTelemetry(unittest.TestCase):

    def test_resume_telemetry_counts_single_and_heading_calls(self):
        candidate = Candidate.model_validate({'candidate_id': 'candidate-a', 'translator_model': 'fake/model:off', 'analyst_model': 'fake/model:off', 'editor_model': 'fake/model:off', 'fast_model': 'fake/model:off', 'pipeline_variant': 'minimal'})
        candidate_spec = CandidateSpec.model_validate({'schema_version': 3, 'benchmark_id': 'phase9', 'temperature': 0.1, 'seed': None, 'replicates': 1, 'candidates': [candidate.model_dump()]})
        records = [_telemetry_record('integration.canary.translate', index=1), _telemetry_record('translate.batch', index=2), _telemetry_record('translate.single', index=3), _telemetry_record('translate.heading', index=4)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.jsonl'
            path.write_text(''.join(json.dumps(record.model_dump()) + '\n' for record in records), encoding='utf-8')
            evidence = telemetry_evidence(path, candidate=candidate, candidate_spec=candidate_spec, start_index=1)
        self.assertTrue(evidence['valid'])
        self.assertEqual(evidence['translate_call_count'], 3)

    def test_telemetry_accepts_exact_agent_translation_operations(self):
        candidate = Candidate.model_validate({'candidate_id': 'candidate-a', 'translator_model': 'fake/model:off', 'analyst_model': 'fake/model:off', 'editor_model': 'fake/model:off', 'fast_model': 'fake/model:off', 'pipeline_variant': 'minimal'})
        candidate_spec = CandidateSpec.model_validate({'schema_version': 3, 'benchmark_id': 'phase9', 'temperature': 0.1, 'seed': None, 'replicates': 1, 'candidates': [candidate.model_dump()]})
        records = [_telemetry_record(operation, agent=agent, index=index) for index, (agent, operation) in enumerate((('translator', 'translate.batch'), ('translator', 'translate.single'), ('analyst', 'translate.heading'), ('analyst', 'translate.single'), ('translator', 'translate.heading'), ('analyst', 'translate.batch'), ('translator', 'translate.unknown'), ('analyst', 'translate.unknown'), ('translator', 'analyzer.analyze')), start=1)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.jsonl'
            path.write_text(''.join(json.dumps(record.model_dump()) + '\n' for record in records), encoding='utf-8')
            evidence = telemetry_evidence(path, candidate=candidate, candidate_spec=candidate_spec)
        self.assertTrue(evidence['valid'])
        self.assertEqual(evidence['translate_call_count'], 6)
        self.assertEqual(evidence['model_mismatch_count'], 5)

    def test_telemetry_accepts_current_and_historical_polish_operations(self):
        candidate = Candidate.model_validate({'candidate_id': 'candidate-a', 'translator_model': 'fake/model:off', 'analyst_model': 'fake/model:off', 'editor_model': 'fake/model:off', 'fast_model': 'fake/model:off', 'pipeline_variant': 'polish'})
        candidate_spec = CandidateSpec.model_validate({'schema_version': 3, 'benchmark_id': 'phase9', 'temperature': 0.1, 'seed': None, 'replicates': 1, 'candidates': [candidate.model_dump()]})
        records = [_telemetry_record(operation, agent='editor', index=index) for index, operation in enumerate(('polish.batch', 'polish.segment'), start=1)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.jsonl'
            path.write_text(''.join(json.dumps(record.model_dump()) + '\n' for record in records), encoding='utf-8')
            evidence = telemetry_evidence(path, candidate=candidate, candidate_spec=candidate_spec)
        self.assertTrue(evidence['valid'])
        self.assertEqual(evidence['model_mismatch_count'], 0)

    def test_telemetry_accepts_chapter_classification_on_analyst(self):
        candidate = Candidate.model_validate({'candidate_id': 'candidate-a', 'translator_model': 'fake/model:off', 'analyst_model': 'fake/model:off', 'editor_model': 'fake/model:off', 'fast_model': 'fake/model:off', 'pipeline_variant': 'minimal'})
        candidate_spec = CandidateSpec.model_validate({'schema_version': 3, 'benchmark_id': 'phase9', 'temperature': 0.1, 'seed': None, 'replicates': 1, 'candidates': [candidate.model_dump()]})
        legitimate = _telemetry_record('chapter.classify', agent='analyst', index=1)
        wrong_agent = _telemetry_record('chapter.classify', agent='preparer', index=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.jsonl'
            path.write_text(json.dumps(legitimate.model_dump()) + '\n', encoding='utf-8')
            legitimate_evidence = telemetry_evidence(path, candidate=candidate, candidate_spec=candidate_spec)
            path.write_text(''.join(json.dumps(record.model_dump()) + '\n' for record in (legitimate, wrong_agent)), encoding='utf-8')
            mixed_evidence = telemetry_evidence(path, candidate=candidate, candidate_spec=candidate_spec)
        self.assertEqual(legitimate_evidence['model_mismatch_count'], 0)
        self.assertEqual(legitimate_evidence['translate_call_count'], 0)
        self.assertEqual(mixed_evidence['model_mismatch_count'], 1)
        self.assertEqual(mixed_evidence['translate_call_count'], 0)

    def test_translator_call_count_counts_first_attempts_across_clients(self):
        first_client_attempt = _telemetry_record('translate.batch', index=1)
        second_client_attempt = _telemetry_record('translate.batch', index=1)
        provider_retry = first_client_attempt.model_copy(update={'attempt_index': 2})
        self.assertEqual(translator_call_count([first_client_attempt, provider_retry]), 1)
        self.assertEqual(translator_call_count([first_client_attempt, second_client_attempt, provider_retry]), 2)
if __name__ == '__main__':
    unittest.main()

# fmt: on
