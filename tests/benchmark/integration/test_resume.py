# fmt: off

"""Benchmark integration observable contracts."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from tests.fixtures.books import write_sample_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.benchmark.artifacts import sha256_bytes
from trans_novel.benchmark.integration import (
    BenchmarkInterruption,
    IntegrationRunner,
    IntegrationSpec,
)
from trans_novel.benchmark.integration.artifacts import IntegrationError
from trans_novel.benchmark.integration.resume import TRANSLATOR_OPERATIONS
from trans_novel.benchmark.schema import CandidateSpec
from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.llm import FakeClient
from trans_novel.llm.telemetry import CallAttemptTelemetry
from trans_novel.model_profiles import parse_model_selection, parse_provider_model
from trans_novel.pipeline import Application
from trans_novel.pipeline.execution import RequiredNodeFailed
from trans_novel.pipeline.state import RUN_INPUT_SCHEMA_VERSION, RunStore


def _spec(**updates):
    value = {'schema_version': 1, 'benchmark_id': 'phase9', 'corpus_sha256': 'a' * 64, 'candidate_spec_sha256': 'b' * 64, 'book_id': 'hidden-book', 'candidate_ids': ['candidate-a', 'candidate-b'], 'interrupt_after_committed_batches': 1, 'output_mono': True, 'output_bilingual': True, 'bilingual_order': 'target_first', 'source_language': 'en', 'target_language': 'zh'}
    value.update(updates)
    return value

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
        self.telemetry_sink.record(CallAttemptTelemetry(schema_version=1, logical_call_id=f'{self._attempts:032x}', attempt_index=1, started_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'), elapsed_ms=0, stage=stage, agent=agent, operation=operation, provider=provider, requested_model=selection.model, resolved_model=selection.model, reasoning_enabled=False, reasoning_effort=None, temperature=0.1, seed=None, json_mode=json_mode, max_tokens=max_tokens, status='success', retry_class=None, http_status=None, finish_reason=None, response_id=None, prompt_tokens=0, completion_tokens=0, total_tokens=0, cache_hit_tokens=0, cache_miss_tokens=0, reasoning_tokens=0, billed_usage_unknown=False, request_sha256='a' * 64, response_sha256=hashlib.sha256(response.encode()).hexdigest()))
        return response

class _StopAfterBatch:

    def __init__(self, count: int=1):
        self.count, self.commits = (count, [])

    def after_batch_committed(self, chapter_index: int, start: int, count: int) -> None:
        self.commits.append((chapter_index, start, count))
        if len(self.commits) >= self.count:
            raise BenchmarkInterruption('test boundary')

def _config(state: Path) -> Config:
    config = Config.from_dict({'llm': fake_llm_dict(), 'quality': 'quality'})
    config.source_lang = 'en'
    config.target_lang = 'zh'
    config.state_dir = str(state)
    return config

def _source(path: Path) -> None:
    path.write_text('First paragraph.\n\nSecond paragraph.\n\nThird paragraph.', encoding='utf-8')

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

class TestBenchmarkIntegrationResume(unittest.TestCase):

    def _runner_fixture(self, root: Path, *, interrupt: int=1):
        source = root / 'hidden.epub'
        if not source.exists():
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

    def test_cli_distinguishes_fresh_resumed_noop_and_failed_reasons(self):
        cli_runner = CliRunner()
        command = ['tools', 'benchmark', 'integration', 'run', 'corpus', 'book.yaml', 'candidates.yaml', 'integration.yaml', '--out', 'out']
        for payload, expected in (({'no_op': False, 'resumed': False, 'failed_candidates': []}, 'fresh'), ({'no_op': False, 'resumed': True, 'failed_candidates': []}, 'resumed'), ({'no_op': True, 'resumed': True, 'failed_candidates': []}, 'no-op')):
            with self.subTest(expected=expected), mock.patch.object(IntegrationRunner, 'run', return_value=payload):
                result = cli_runner.invoke(app, command)
            self.assertEqual(result.exit_code, 0)
            self.assertIn(f'Integration {expected}', result.output)
        with mock.patch.object(IntegrationRunner, 'run', return_value={'no_op': False, 'resumed': False, 'failed_candidates': ['candidate-a']}):
            result = cli_runner.invoke(app, command)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('candidate-a', result.output)

    def test_default_application_behavior_has_no_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            client = FakeClient(handler=routing_handler)
            result = Application(_config(root / 'state'), client=client).run_all(str(source), out_format='txt')
            self.assertIsNotNone(result['store'])
            self.assertFalse(any(call.get('operation') == 'integration.canary.translate' for call in client.calls))

    def test_existing_state_without_request_refuses_before_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, clients = self._runner_fixture(root)
            (root / 'out' / 'candidates').mkdir(parents=True)
            with self.assertRaises(IntegrationError):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            self.assertEqual(clients, [])

    def test_fabricated_telemetry_and_slice_tamper_refuse_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value={'structural_pass': True, 'mono': {'structural_pass': True}, 'bilingual': {'structural_pass': True}}):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            telemetry = root / 'out' / 'candidates' / 'candidate-a-polished' / 'telemetry.jsonl'
            telemetry.write_text(telemetry.read_text() + '{"usage_known":true}\n', encoding='utf-8')
            with self.assertRaises(IntegrationError):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')

    def test_failed_candidate_continues_and_failed_completion_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            calls = {'count': 0}

            def failing_factory(**_kwargs):
                calls['count'] += 1
                return FakeClient(handler=(lambda *_args: (_ for _ in ()).throw(RuntimeError('bad'))) if calls['count'] == 1 else routing_handler)
            runner.client_factory = failing_factory
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value={'structural_pass': True, 'mono': {'structural_pass': True}, 'bilingual': {'structural_pass': True}}):
                value = runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            self.assertIn('candidate-a-polished', value['failed_candidates'])
            self.assertIn('candidate-b-polished', value['candidates'])
            again = runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            self.assertTrue(again['no_op'])
            self.assertIn('candidate-a-polished', again['failed_candidates'])

    def test_false_benchmark_interruption_is_failed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            canary = {'schema_version': 1, 'passed': True, 'reasoning_tokens': 0, 'model_mismatch_count': 0, 'unknown_required_usage_count': 0}
            with mock.patch('trans_novel.benchmark.integration.resume.run_canary', return_value=canary), mock.patch.object(Application, 'run_all', side_effect=BenchmarkInterruption('false')):
                value = runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            self.assertEqual(len(value['failed_candidates']), 2)
            self.assertTrue(all('IntegrationError' in json.loads(path.read_text()).get('failure_reasons', []) for path in (root / 'out').glob('candidates/*/result.json')))

    def test_hook_does_not_fire_for_already_translated_quality_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            first = Application(_config(root / 'state'), client=FakeClient(handler=routing_handler)).run_all(str(source), out_format='txt')
            target_before = Path(first['store'].chapter_path(0)).read_bytes()
            hook = _StopAfterBatch()
            resumed = FakeClient(handler=routing_handler)
            second = Application(_config(root / 'state'), client=resumed, batch_commit_hook=hook).run_all(str(source), out_format='txt')
            self.assertEqual(hook.commits, [])
            self.assertEqual(Path(second['store'].chapter_path(0)).read_bytes(), target_before)
            self.assertEqual(sum(call['operation'] == 'translate.batch' for call in resumed.calls), 0)

    def test_hook_persists_actual_translation_request_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            hook = _StopAfterBatch()
            client = FakeClient(handler=routing_handler)
            with self.assertRaises(BenchmarkInterruption):
                Application(_config(root / 'state'), client=client, batch_commit_hook=hook).run(str(source))
            self.assertEqual(len(hook.commits), 1)
            event_files = list((root / 'state').rglob('events.jsonl'))
            self.assertEqual(len(event_files), 1)
            events = [json.loads(line) for line in event_files[0].read_text(encoding='utf-8').splitlines()]
            batch_event = next(event for event in events if event['event'] == 'batch_translated')
            self.assertEqual(batch_event['translate_call_count'], sum(call['operation'] in TRANSLATOR_OPERATIONS for call in client.calls))

    def test_interrupted_event_prefix_and_slice_tamper_refuse_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            integration_module = __import__('trans_novel.benchmark.integration.resume', fromlist=['write_integration_json'])
            original_write = integration_module.write_integration_json

            def stop_after_interrupted(path, value):
                original_write(path, value)
                if Path(path).name == 'integration_state.json' and any(item.get('status') == 'interrupted' for item in value.get('candidates', {}).values()):
                    raise SystemExit('durable interruption')
            with mock.patch('trans_novel.benchmark.integration.resume.write_integration_json', side_effect=stop_after_interrupted), self.assertRaises(SystemExit):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            events = next((root / 'out' / 'candidates' / 'candidate-a-polished' / 'state').glob('*/events.jsonl'))
            events.write_text('X' + events.read_text(), encoding='utf-8')
            runner2, _source2, _clients2 = self._runner_fixture(root)
            with self.assertRaises(IntegrationError):
                runner2.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')

    def test_interrupted_state_carries_restart_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value={'structural_pass': True, 'mono': {'structural_pass': True}, 'bilingual': {'structural_pass': True}}):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            state = json.loads((root / 'out' / 'integration_state.json').read_text())
            for entry in state['candidates'].values():
                self.assertIn('interruption', entry)
                self.assertIn('before_target_hashes', entry)
                self.assertIn('canary_sha256', entry)
                self.assertIn('boundary_event_count', entry)

    def test_new_application_resumes_without_retranslating_committed_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            with self.assertRaises(BenchmarkInterruption):
                Application(_config(root / 'state'), client=FakeClient(handler=routing_handler), batch_commit_hook=_StopAfterBatch()).run(str(source))
            resumed = FakeClient(handler=routing_handler)
            result = Application(_config(root / 'state'), client=resumed).run(str(source))
            self.assertIsNotNone(result)
            events_path = next((root / 'state').rglob('events.jsonl'))
            events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(event.get('reason') == 'already_translated' for event in events))
            self.assertEqual(sum(call['operation'] == 'translate.batch' for call in resumed.calls), 0)

class TestBenchmarkIntegrationResumeContinuation(unittest.TestCase):
    _runner_fixture = TestBenchmarkIntegrationResume._runner_fixture

    def test_public_runner_fakeclient_workflow_restart_and_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source_path, clients = self._runner_fixture(root)
            structural = {'schema_version': 1, 'structural_pass': True, 'source': {'structural_pass': True}, 'mono': {'structural_pass': True}, 'bilingual': {'structural_pass': True}}
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value=structural):
                first = runner.run(root, root / 'books.yaml', root / 'candidates.yaml', root / 'integration.yaml', root / 'out')
            self.assertFalse(first['no_op'])
            self.assertEqual(sorted(first['failed_candidates']), [])
            self.assertTrue(first['resumed'] is False)
            self.assertEqual(len(clients), 6)
            request = json.loads((root / 'out' / 'integration_request.json').read_text(encoding='utf-8'))
            self.assertEqual({item['pipeline_variant'] for item in request['candidates'].values()}, {'polish'})
            for cid in ('candidate-a-polished', 'candidate-b-polished'):
                result = json.loads((root / 'out' / 'candidates' / cid / 'result.json').read_text())
                self.assertTrue(result['expected_interruption_observed'])
                self.assertTrue(result['readiness_passed'])
                self.assertEqual(result['resume_duplicate_operations'], 0)
                self.assertGreaterEqual(result['remaining_batches'], 0)
                self.assertTrue(result['telemetry_sha256'])
                self.assertTrue(result['usage_sha256'])
            rerun = runner.run(root, root / 'books.yaml', root / 'candidates.yaml', root / 'integration.yaml', root / 'out')
            self.assertTrue(rerun['no_op'])
            self.assertEqual(len(clients), 6)

    def test_public_runner_restart_after_durable_interrupted_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, clients = self._runner_fixture(root)
            integration_module = __import__('trans_novel.benchmark.integration.resume', fromlist=['write_integration_json'])
            original_write = integration_module.write_integration_json

            def stop_after_interrupted(path, value):
                original_write(path, value)
                if Path(path).name == 'integration_state.json' and any(item.get('status') == 'interrupted' for item in value.get('candidates', {}).values()):
                    raise SystemExit('durable interruption')
            with mock.patch('trans_novel.benchmark.integration.resume.write_integration_json', side_effect=stop_after_interrupted), self.assertRaises(SystemExit):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            self.assertEqual(len(clients), 2)
            source_bytes = _source.read_bytes()
            runner2, _source2, clients2 = self._runner_fixture(root)
            self.assertEqual(_source2.read_bytes(), source_bytes)
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value={'structural_pass': True, 'mono': {'structural_pass': True}, 'bilingual': {'structural_pass': True}}):
                result = runner2.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            self.assertFalse(result['failed_candidates'])
            self.assertEqual(len(clients2), 4)
            self.assertFalse(any(call['operation'].startswith('integration.canary') for call in clients2[0].calls))

    def test_readiness_assignment_is_derived_from_production_problems(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value={'structural_pass': False, 'mono': {'structural_pass': False}, 'bilingual': {'structural_pass': False}}):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            result = json.loads((root / 'out' / 'candidates' / 'candidate-a-polished' / 'result.json').read_text())
            self.assertEqual(result['readiness_passed'], result['readiness_problem_count'] == 0)
            self.assertNotIn('readiness_problems', result)

    def test_required_event_failure_prevents_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            hook = _StopAfterBatch()
            with mock.patch.object(RunStore, 'log_event_required', side_effect=OSError('append failed')), self.assertRaises(RequiredNodeFailed):
                Application(_config(root / 'state'), client=FakeClient(handler=routing_handler), batch_commit_hook=hook).run(str(source))
            self.assertEqual(hook.commits, [])

    def test_restart_after_crash_following_resumed_batches_reattributes_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source_path, _clients = self._runner_fixture(root)
            integration_resume = __import__('trans_novel.benchmark.integration.resume', fromlist=['Application'])
            original_run_all = integration_resume.Application.run_all
            calls = {'count': 0}

            def crash_after_resume(app, *args, **kwargs):
                calls['count'] += 1
                value = original_run_all(app, *args, **kwargs)
                if calls['count'] == 2:
                    raise SystemExit('crash after resumed batches')
                return value
            with mock.patch.object(integration_resume.Application, 'run_all', crash_after_resume), self.assertRaises(SystemExit):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            prior_state = json.loads((root / 'out' / 'integration_state.json').read_text())
            prior_cumulative = prior_state['candidates']['candidate-a-polished'].get('resume_wall_ms', 0)
            source_bytes = _source_path.read_bytes()
            runner2 = IntegrationRunner(client_factory=runner.client_factory)
            self.assertEqual(_source_path.read_bytes(), source_bytes)
            structural = {'schema_version': 1, 'structural_pass': True, 'source': {'structural_pass': True}, 'mono': {'structural_pass': True}, 'bilingual': {'structural_pass': True}}
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value=structural):
                result = runner2.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            self.assertFalse(result['failed_candidates'])
            recovered = json.loads((root / 'out' / 'candidates' / 'candidate-a-polished' / 'result.json').read_text())
            state = json.loads((root / 'out' / 'integration_state.json').read_text())
            recovered_state = state['candidates']['candidate-a-polished']['recovered_resume']
            self.assertGreater(recovered_state['active_duration_ms'], 0)
            durations = state['candidates']['candidate-a-polished']['resume_durations_ms']
            self.assertTrue(all(duration > 0 for duration in durations))
            self.assertEqual(state['candidates']['candidate-a-polished']['resume_wall_ms'], sum(durations))
            self.assertGreaterEqual(state['candidates']['candidate-a-polished']['resume_wall_ms'], prior_cumulative)
            timings = recovered['phase_timings_ms']
            self.assertEqual(timings['total'], timings['first_attempt'] + timings['resume'])
            self.assertEqual(recovered_state['active_duration_ms'], durations[0])

    def test_resume_timing_is_cumulative_and_partitioned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source_path, _clients = self._runner_fixture(root)
            structural = {'schema_version': 1, 'structural_pass': True, 'source': {'structural_pass': True}, 'mono': {'structural_pass': True}, 'bilingual': {'structural_pass': True}}
            with mock.patch('trans_novel.benchmark.integration.resume.validate_epub_triplet', return_value=structural):
                runner.run(root, root / 'b.yaml', root / 'c.yaml', root / 'i.yaml', root / 'out')
            state = json.loads((root / 'out' / 'integration_state.json').read_text())
            for cid, entry in state['candidates'].items():
                timings = json.loads((root / 'out' / 'candidates' / cid / 'result.json').read_text())['phase_timings_ms']
                self.assertEqual(timings['total'], timings['first_attempt'] + timings['resume'])
                self.assertEqual(entry['resume_wall_ms'], timings['resume'])
                self.assertEqual(sum(entry['resume_durations_ms']), timings['resume'])

    def test_resumed_fallback_event_matches_actual_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            config = _config(root / 'state')
            config.segment.max_chars_per_batch = 40
            config.pipeline.polish = False
            with self.assertRaises(BenchmarkInterruption):
                Application(config, client=FakeClient(handler=routing_handler), batch_commit_hook=_StopAfterBatch()).run(str(source))

            def fallback_handler(messages, agent, operation, json_mode):
                if operation == 'title.translate':
                    return json.dumps({'titles': ['标题']}, ensure_ascii=False)
                if agent == 'translator':
                    return '译' * 300
                return '分析译文'
            resumed = FakeClient(handler=fallback_handler)
            Application(config, client=resumed).run(str(source))
            events_path = next((root / 'state').rglob('events.jsonl'))
            events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
            translated_events = [event for event in events if event['event'] == 'batch_translated']
            resumed_event = translated_events[-1]
            resumed_requests = sum(call['operation'] in TRANSLATOR_OPERATIONS for call in resumed.calls)
            self.assertEqual(resumed_event['translate_call_count'], resumed_requests)
            self.assertEqual(resumed_event['translate_call_count'], 4)

    def test_translator_model_change_invalidates_translation_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            first = Application(_config(root / 'state'), client=FakeClient(handler=routing_handler)).run(str(source))
            old_name_fingerprint = first.load_state().nodes['name_terms'].input_fingerprint
            changed = _config(root / 'state')
            changed.llm.models.translator = ['fake/different-translator']
            client = FakeClient(handler=routing_handler)
            second = Application(changed, client=client).run(str(source))
            self.assertEqual(second.load_state().nodes['name_terms'].input_fingerprint, old_name_fingerprint)
            self.assertGreater(sum(call['operation'] in TRANSLATOR_OPERATIONS for call in client.calls), 0)
            events_path = next((root / 'state').rglob('events.jsonl'))
            events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(event.get('event') == 'translate_invalidated' for event in events))

    def test_unexpected_exception_is_not_treated_as_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.txt'
            _source(source)
            client = FakeClient(handler=lambda *args: (_ for _ in ()).throw(RuntimeError('boom')))
            with self.assertRaises(RequiredNodeFailed):
                Application(_config(root / 'state'), client=client).run(str(source))
if __name__ == '__main__':
    unittest.main()

# fmt: on
