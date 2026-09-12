"""Benchmark integration observable contracts."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures.books import write_sample_epub
from tests.fixtures.fake_llm import routing_handler
from trans_novel.benchmark.artifacts import canonical_json, sha256_bytes
from trans_novel.benchmark.integration import (
    IntegrationRunner,
    IntegrationSpec,
)
from trans_novel.benchmark.integration.artifacts import (
    IntegrationError,
)
from trans_novel.benchmark.integration.resume import (
    telemetry_evidence,
)
from trans_novel.benchmark.schema import Candidate, CandidateSpec
from trans_novel.llm import FakeClient
from trans_novel.llm.telemetry import CallAttemptTelemetry
from trans_novel.model_profiles import parse_model_selection, parse_provider_model
from trans_novel.pipeline import Application
from trans_novel.pipeline.state import RUN_INPUT_SCHEMA_VERSION


def _spec(**updates):
    value = {
        "schema_version": 1,
        "benchmark_id": "phase9",
        "corpus_sha256": "a" * 64,
        "candidate_spec_sha256": "b" * 64,
        "book_id": "hidden-book",
        "candidate_ids": ["candidate-a", "candidate-b"],
        "interrupt_after_committed_batches": 1,
        "output_mono": True,
        "output_bilingual": True,
        "bilingual_order": "target_first",
        "source_language": "en",
        "target_language": "zh",
    }
    value.update(updates)
    return value


class _InstrumentedFakeClient(FakeClient):
    def __init__(
        self, *, handler, models: tuple[str, str, str, str], provider: str = "fake"
    ) -> None:
        super().__init__(handler=handler)
        self.models, self.provider, self.telemetry_sink, self._attempts = models, provider, None, 0

    def set_telemetry_sink(self, sink) -> None:
        self.telemetry_sink = sink

    def complete(self, messages, *, json_mode=False, max_tokens=None, stage=None, agent, operation):
        response = super().complete(
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
            agent=agent,
            operation=operation,
        )
        self._attempts += 1
        model_ref = (
            self.models[1]
            if agent == "analyst"
            else self.models[2]
            if agent == "editor"
            else self.models[3]
            if agent == "preparer"
            else self.models[0]
        )
        provider, model = parse_provider_model(model_ref)
        selection = parse_model_selection(model)
        self.telemetry_sink.record(
            CallAttemptTelemetry(
                schema_version=1,
                logical_call_id=f"{self._attempts:032x}",
                attempt_index=1,
                started_at="2026-01-01T00:00:00.000Z",
                elapsed_ms=0,
                stage=stage,
                agent=agent,
                operation=operation,
                provider=provider,
                requested_model=selection.model,
                resolved_model=selection.model,
                reasoning_enabled=False,
                reasoning_effort=None,
                temperature=0.1,
                seed=None,
                json_mode=json_mode,
                max_tokens=max_tokens,
                status="success",
                retry_class=None,
                http_status=None,
                finish_reason=None,
                response_id=None,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cache_hit_tokens=0,
                cache_miss_tokens=0,
                reasoning_tokens=0,
                billed_usage_unknown=False,
                request_sha256="a" * 64,
                response_sha256=hashlib.sha256(response.encode()).hexdigest(),
            )
        )
        return response


def _artifact_hash(corpus: dict, runner: list[dict], challenge_keys: list[dict]) -> str:
    semantics = {
        "corpus": {key: value for key, value in corpus.items() if key != "corpus_sha256"},
        "runner_segments": runner,
        "challenge_keys": challenge_keys,
    }
    encoded = json.dumps(semantics, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_minimal_artifact(
    root: Path,
    runner: list[dict],
    *,
    manifest_books: list[dict] | None = None,
    challenge_keys: list[dict] | None = None,
) -> None:
    challenge_keys = challenge_keys or []
    manifest_books = manifest_books or [
        {
            "book_id": "book",
            "source_sha256": "a" * 64,
            "basename": "book.txt",
            "split": "screen",
            "format": "text",
            "title": "book",
            "chapter_count": 1,
            "parser_schema": RUN_INPUT_SCHEMA_VERSION,
        }
    ]
    corpus = {
        "schema_version": 1,
        "benchmark_name": "fixture",
        "word_counter": "en-v1",
        "parser_schema": RUN_INPUT_SCHEMA_VERSION,
        "run_input_schema_version": RUN_INPUT_SCHEMA_VERSION,
        "books": manifest_books,
        "passages": [
            {
                key: row[key]
                for key in (
                    "passage_id",
                    "subset",
                    "book_id",
                    "chapter_index",
                    "start",
                    "end",
                    "word_count",
                    "strata",
                )
            }
            for row in runner
        ],
        "quotas": {
            "targets": {
                "screen": 10_000,
                "continuous": 30_000,
                "stratified": 15_000,
                "context": 5_000,
            },
            "actual": {
                "screen": 0,
                "continuous": 0,
                "stratified": 0,
                "context": 0,
                "hidden": 0,
                "formal": 0,
            },
            "tolerance": 0.2,
        },
    }
    corpus["corpus_sha256"] = _artifact_hash(corpus, runner, challenge_keys)
    (root / "corpus.json").write_text(
        json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (root / "source_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_input_schema_version": RUN_INPUT_SCHEMA_VERSION,
                "books": manifest_books,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "runner_segments.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in runner
        ),
        encoding="utf-8",
    )
    (root / "challenge_keys.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in challenge_keys
        ),
        encoding="utf-8",
    )


class TestBenchmarkIntegrationArtifacts(unittest.TestCase):
    def _runner_fixture(self, root: Path, *, interrupt: int = 1):
        source = root / "hidden.epub"
        write_sample_epub(str(source))
        candidate_spec = CandidateSpec.model_validate(
            {
                "schema_version": 3,
                "benchmark_id": "phase9",
                "temperature": 0.1,
                "seed": None,
                "replicates": 1,
                "candidates": [
                    {
                        "candidate_id": "candidate-a-polished",
                        "translator_model": "bailian/qwen3.8-max:off",
                        "analyst_model": "bailian/qwen3.7-flash:off",
                        "editor_model": "bailian/deepseek-v4-pro:off",
                        "fast_model": "bailian/qwen3.7-flash:off",
                        "pipeline_variant": "polish",
                    },
                    {
                        "candidate_id": "candidate-b-polished",
                        "translator_model": "bailian/deepseek-v4-flash:off",
                        "analyst_model": "bailian/qwen3.7-flash:off",
                        "editor_model": "bailian/qwen3.7-plus:off",
                        "fast_model": "bailian/qwen3.7-flash:off",
                        "pipeline_variant": "polish",
                    },
                ],
            }
        )
        selected = list(candidate_spec.candidates)
        integration_spec = IntegrationSpec.model_validate(
            _spec(
                candidate_ids=["candidate-a-polished", "candidate-b-polished"],
                interrupt_after_committed_batches=interrupt,
            )
        )
        lineage = {
            "book_spec_sha256": "d" * 64,
            "candidate_spec_sha256": "b" * 64,
            "integration_spec_sha256": "e" * 64,
            "selected": selected,
            "source_sha256": sha256_bytes(source.read_bytes()),
        }
        factory_calls: list[FakeClient] = []

        def factory(**kwargs):
            roles = kwargs.get("models")
            if roles is not None:
                models = (
                    roles.translator[0],
                    roles.analyst[0],
                    roles.editor[0],
                    roles.fast[0],
                )
            else:
                if len(factory_calls) < 3:
                    models = (
                        "bailian/qwen3.8-max:off",
                        "bailian/qwen3.7-flash:off",
                        "bailian/deepseek-v4-pro:off",
                        "bailian/qwen3.7-flash:off",
                    )
                else:
                    models = (
                        "bailian/deepseek-v4-flash:off",
                        "bailian/qwen3.7-flash:off",
                        "bailian/qwen3.7-plus:off",
                        "bailian/qwen3.7-flash:off",
                    )
            client = _InstrumentedFakeClient(
                handler=routing_handler, models=models, provider="bailian"
            )
            factory_calls.append(client)
            return client

        runner = IntegrationRunner(client_factory=factory)
        preflight_patch = mock.patch(
            "trans_novel.benchmark.integration.preflight",
            return_value=(
                integration_spec,
                candidate_spec,
                source,
                lineage["source_sha256"],
                lineage,
            ),
        )
        preflight_patch.start()
        self.addCleanup(preflight_patch.stop)
        return runner, source, factory_calls

    def test_canary_operation_agent_tamper_refuses_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source_path, _clients = self._runner_fixture(root)
            structural = {
                "schema_version": 1,
                "structural_pass": True,
                "source": {"structural_pass": True},
                "mono": {"structural_pass": True},
                "bilingual": {"structural_pass": True},
            }
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value=structural,
            ):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            telemetry = root / "out" / "candidates" / "candidate-a-polished" / "telemetry.jsonl"
            rows = [json.loads(line) for line in telemetry.read_text().splitlines()]
            rows[0]["operation"] = "integration.canary.wrong"
            telemetry.write_text(
                "".join(canonical_json(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            with self.assertRaises(IntegrationError):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")

    def test_canary_passes_real_telemetry_and_records_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value={
                    "structural_pass": True,
                    "mono": {"structural_pass": True},
                    "bilingual": {"structural_pass": True},
                },
            ):
                result = runner.run(
                    root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out"
                )
            self.assertTrue(result["candidates"]["candidate-a-polished"]["result_path"])
            evidence = json.loads(
                (root / "out" / "candidates" / "candidate-a-polished" / "result.json").read_text()
            )
            self.assertTrue(evidence["canary_passed"])
            self.assertEqual(evidence["model_mismatch_count"], 0)
            self.assertEqual(evidence["reasoning_tokens"], 0)
            self.assertEqual(evidence["unknown_required_usage_count"], 0)

    def test_canary_wrong_model_reasoning_and_unknown_usage_fail_public_run(self):
        for mutation, field in (
            ("wrong-model", "model_mismatch_count"),
            ("reasoning", "reasoning_tokens"),
            ("unknown", "unknown_required_usage_count"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner, _source, _clients = self._runner_fixture(root)
                from trans_novel.benchmark.integration.canary import run_canary

                def altered(*args, _original=run_canary, _field=field, **kwargs):
                    value = _original(*args, **kwargs)
                    value["passed"] = False
                    value[_field] = 1
                    return value

                with mock.patch(
                    "trans_novel.benchmark.integration.resume.run_canary", side_effect=altered
                ):
                    runner.run(
                        root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out"
                    )
                result = json.loads(
                    (
                        root / "out" / "candidates" / "candidate-a-polished" / "result.json"
                    ).read_text()
                )
                self.assertFalse(result["passed"])
                self.assertGreaterEqual(result[field], 1)

    def test_canonical_manifest_hash_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integration = {
                "schema_version": 1,
                "benchmark_id": "phase9",
                "corpus_sha256": "a" * 64,
                "candidates": {},
            }
            payload = (
                json.dumps(integration, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
            path = root / "integration.json"
            path.write_text(payload, encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def test_completion_tamper_noncanonical_and_missing_refuse_before_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, clients = self._runner_fixture(root)
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value={
                    "structural_pass": True,
                    "mono": {"structural_pass": True},
                    "bilingual": {"structural_pass": True},
                },
            ):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            integration = root / "out" / "integration.json"
            integration.write_text(
                integration.read_text().replace('"schema_version":1', '"schema_version": 1'),
                encoding="utf-8",
            )
            with self.assertRaises(IntegrationError):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            self.assertEqual(len(clients), 6)

    def test_cross_candidate_and_bilingual_aliases_refuse_before_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, source, clients = self._runner_fixture(root)
            out = root / "out"
            first = out / "candidates" / "candidate-a-polished"
            first.mkdir(parents=True)
            (first / "outputs").mkdir()
            (first / "outputs" / "hidden-bi.epub").symlink_to(source)
            (out / "candidates" / "candidate-b-polished").symlink_to(
                first, target_is_directory=True
            )
            with self.assertRaises(IntegrationError):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", out)
            self.assertEqual(clients, [])

    def test_failed_result_schema_tamper_refuses_terminal_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            runner.client_factory = lambda **_kwargs: FakeClient(
                handler=lambda *_args: (_ for _ in ()).throw(RuntimeError("bad"))
            )
            runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            result_path = root / "out" / "candidates" / "candidate-a-polished" / "result.json"
            value = json.loads(result_path.read_text())
            value["book_id"] = "tampered"
            result_path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            with self.assertRaises(IntegrationError):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")

    def test_integration_runner_refuses_mismatched_existing_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / "integration_request.json"
            request.write_text('{"benchmark_id":"other"}\n', encoding="utf-8")
            runner = IntegrationRunner()
            with (
                mock.patch(
                    "trans_novel.benchmark.integration.preflight",
                    return_value=(
                        IntegrationSpec.model_validate(_spec()),
                        mock.Mock(),
                        root / "book.epub",
                        "c" * 64,
                        {
                            "book_spec_sha256": "d" * 64,
                            "candidate_spec_sha256": "b" * 64,
                            "integration_spec_sha256": "e" * 64,
                            "selected": [],
                            "source_sha256": "c" * 64,
                        },
                    ),
                ),
                self.assertRaises(IntegrationError),
            ):
                runner.run(
                    root,
                    root / "book.yaml",
                    root / "candidates.yaml",
                    root / "integration.yaml",
                    root,
                )


class TestBenchmarkIntegrationArtifactsValidation(unittest.TestCase):
    _runner_fixture = TestBenchmarkIntegrationArtifacts._runner_fixture

    def test_missing_physical_telemetry_or_usage_fails_with_unknown_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            original_run = Application.run_all
            calls = {"count": 0}

            def remove_after_resume(app, *args, **kwargs):
                calls["count"] += 1
                value = original_run(app, *args, **kwargs)
                if calls["count"] == 2:
                    (
                        root / "out" / "candidates" / "candidate-a-polished" / "telemetry.jsonl"
                    ).unlink()
                return value

            with (
                mock.patch.object(Application, "run_all", remove_after_resume),
                mock.patch(
                    "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                    return_value={
                        "structural_pass": True,
                        "mono": {"structural_pass": True},
                        "bilingual": {"structural_pass": True},
                    },
                ),
            ):
                result = runner.run(
                    root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out"
                )
            self.assertIn("candidate-a-polished", result["failed_candidates"])
            failed = json.loads(
                (root / "out" / "candidates" / "candidate-a-polished" / "result.json").read_text()
            )
            self.assertGreater(failed["unknown_required_usage_count"], 0)

    def test_noop_forged_predicate_and_missing_timing_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value={
                    "structural_pass": True,
                    "mono": {"structural_pass": True},
                    "bilingual": {"structural_pass": True},
                },
            ):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            result_path = root / "out" / "candidates" / "candidate-a-polished" / "result.json"
            value = json.loads(result_path.read_text())
            value["passed"] = True
            value.pop("phase_timings_ms", None)
            result_path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            with self.assertRaises(IntegrationError):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")

    def test_request_state_result_completion_tamper_and_missing_refuse(self):
        for artifact in ("request", "state", "result", "completion"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner, _source, _clients = self._runner_fixture(root)
                with mock.patch(
                    "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                    return_value={
                        "structural_pass": True,
                        "mono": {"structural_pass": True},
                        "bilingual": {"structural_pass": True},
                    },
                ):
                    runner.run(
                        root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out"
                    )
                out = root / "out"
                paths = {
                    "request": out / "integration_request.json",
                    "state": out / "integration_state.json",
                    "result": out / "candidates" / "candidate-a-polished" / "result.json",
                    "completion": out / "integration_complete.json",
                }
                path = paths[artifact]
                if artifact == "completion":
                    path.unlink()
                elif artifact == "state":
                    path.write_text('{"schema_version": 1}\n', encoding="utf-8")
                else:
                    path.write_text(
                        path.read_text(encoding="utf-8").replace("phase9", "phaseX", 1),
                        encoding="utf-8",
                    )
                with self.assertRaises(IntegrationError):
                    runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", out)

    def test_singleton_factory_is_rejected_before_second_client_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            singleton = _InstrumentedFakeClient(
                handler=routing_handler,
                models=("fake/primary-a:off", "fake/editor-a:off", "fake/fast:off"),
            )
            runner.client_factory = lambda **_kwargs: singleton
            with self.assertRaises(IntegrationError):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")

    def test_strict_provider_temperature_operation_and_usage_evidence_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            with (
                mock.patch(
                    "trans_novel.benchmark.integration.preflight",
                    side_effect=IntegrationError("provider/temp/operation gate"),
                ) as preflight_mock,
                self.assertRaises(IntegrationError),
            ):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            self.assertTrue(preflight_mock.called)

    def test_telemetry_evidence_rejects_provider_temperature_agent_and_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value={
                    "structural_pass": True,
                    "mono": {"structural_pass": True},
                    "bilingual": {"structural_pass": True},
                },
            ):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            candidate = Candidate.model_validate(
                {
                    "candidate_id": "candidate-a-polished",
                    "translator_model": "bailian/qwen3.8-max:off",
                    "analyst_model": "bailian/qwen3.7-flash:off",
                    "editor_model": "bailian/deepseek-v4-pro:off",
                    "fast_model": "bailian/qwen3.7-flash:off",
                    "pipeline_variant": "polish",
                }
            )
            candidate_spec = CandidateSpec.model_validate(
                {
                    "schema_version": 3,
                    "benchmark_id": "phase9",
                    "temperature": 0.1,
                    "seed": None,
                    "replicates": 1,
                    "candidates": [candidate.model_dump()],
                }
            )
            telemetry = root / "out" / "candidates" / candidate.candidate_id / "telemetry.jsonl"
            row = json.loads(telemetry.read_text().splitlines()[0])
            for field, altered in (
                ("provider", "wrong-provider"),
                ("temperature", 0.2),
                ("agent", "editor"),
                ("operation", "wrong.operation"),
            ):
                with self.subTest(field=field):
                    value = dict(row)
                    value[field] = altered
                    path = root / f"{field}.jsonl"
                    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
                    evidence = telemetry_evidence(
                        path,
                        candidate=candidate,
                        candidate_spec=candidate_spec,
                    )
                    self.assertTrue(evidence["valid"])
                    self.assertGreaterEqual(evidence["model_mismatch_count"], 1)

    def test_terminal_candidates_are_included_when_manifests_regenerate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source_path, clients = self._runner_fixture(root)
            structural = {
                "schema_version": 1,
                "structural_pass": True,
                "source": {"structural_pass": True},
                "mono": {"structural_pass": True},
                "bilingual": {"structural_pass": True},
            }
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value=structural,
            ):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            (root / "out" / "integration.json").unlink()
            (root / "out" / "integration_complete.json").unlink()
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value=structural,
            ):
                regenerated = runner.run(
                    root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out"
                )
            self.assertEqual(
                set(regenerated["candidates"]),
                {"candidate-a-polished", "candidate-b-polished"},
            )
            self.assertEqual(len(clients), 6)

    def test_terminal_state_semantic_prefix_tamper_refuses_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source_path, _clients = self._runner_fixture(root)
            structural = {
                "schema_version": 1,
                "structural_pass": True,
                "source": {"structural_pass": True},
                "mono": {"structural_pass": True},
                "bilingual": {"structural_pass": True},
            }
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value=structural,
            ):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")
            state_path = root / "out" / "integration_state.json"
            state = json.loads(state_path.read_text())
            state["candidates"]["candidate-a-polished"]["before_target_hashes"].append({})
            state_path.write_text(canonical_json(state) + "\n", encoding="utf-8")
            with self.assertRaises(IntegrationError):
                runner.run(root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out")

    def test_triplet_aggregate_false_cannot_be_overridden_by_child_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, _source, _clients = self._runner_fixture(root)
            value = {
                "schema_version": 1,
                "structural_pass": False,
                "source": {"structural_pass": True},
                "mono": {"structural_pass": True},
                "bilingual": {"structural_pass": True},
            }
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet", return_value=value
            ):
                result = runner.run(
                    root, root / "b.yaml", root / "c.yaml", root / "i.yaml", root / "out"
                )
            self.assertTrue(result["failed_candidates"])

    def test_validator_boundary_returns_safe_triplet_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            mono = root / "mono.epub"
            bilingual = root / "bilingual.epub"
            with mock.patch(
                "trans_novel.benchmark.integration.resume.validate_epub_triplet",
                return_value={
                    "schema_version": 1,
                    "structural_pass": True,
                    "source": {"structural_pass": True},
                    "mono": {"structural_pass": True},
                    "bilingual": {"structural_pass": True},
                },
            ) as validator:
                value = validator(source, mono, bilingual)
            self.assertTrue(value["structural_pass"])
            validator.assert_called_once_with(source, mono, bilingual)


if __name__ == "__main__":
    unittest.main()
