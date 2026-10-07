"""Checkpoint three independent drafts and one comprehensive polishing before publication."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

from ..agents.precision import PrecisionAgent, PrecisionError, PrecisionInputs
from ..config import Config
from ..ingest.models import Chapter
from ..llm.base import LLMClient
from ..llm.routing import identity, inference_snapshot
from ..markup.ruby import strip_ruby_markers
from ..storage.precision_archive import PrecisionArchive
from ..storage.protocol import Storage
from .precision_records import PrecisionRecords
from .translation_batch import BatchPlan, BatchResult

_OPERATIONS = ("translation.body", "polish.body")
_VERSION = 3
_DRAFT_IDS = ("T1", "T2", "T3")


class PrecisionBatchExecutor:
    """Own draft concurrency; the chapter service alone publishes synthesized results."""

    def __init__(self, client: LLMClient, config: Config):
        self._client = client
        self._config = config

    def execute(
        self,
        plan: BatchPlan,
        store: Storage,
        *,
        checkpoint: Callable[[], None] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> BatchResult:
        if not self._config.pipeline.polish:
            raise PrecisionError("Best-of-three translation requires polishing")
        return _PrecisionRun(
            self._client, self._config, plan, store, checkpoint=checkpoint, progress=progress
        ).execute()

    @staticmethod
    def prepared_publication(store: Storage, result: BatchResult) -> dict[str, Any]:
        if result.precision_key is None:
            raise PrecisionError("Precision publication requires its prepared index")
        publication = store.read_artifact(f"{result.precision_key}/publication.json")
        if (
            not isinstance(publication, dict)
            or publication.get("status") not in {"ready", "published"}
            or publication.get("target_hash") != identity(result.targets)
            or publication.get("fingerprint") != result.precision_key.rsplit("/", 1)[-1]
            or publication.get("source_sha256") != store.load_manifest().get("source_sha256")
        ):
            raise PrecisionError("Precision publication does not match its prepared index")
        return publication

    @staticmethod
    def mark_published(store: Storage, result: BatchResult) -> None:
        publication = PrecisionBatchExecutor.prepared_publication(store, result)
        store.write_artifact(
            f"{result.precision_key}/publication.json", {**publication, "status": "published"}
        )

    @staticmethod
    def recover_publications(store: Storage, chapter: Chapter) -> None:
        """Reconcile a commit interrupted before its marker, even for an older workflow."""
        segments = {segment.index: segment for segment in chapter.text_segments}
        source_hash = store.load_manifest().get("source_sha256")
        for key in store.list_artifacts(f"precision/chapters/{chapter.index}/"):
            if not key.endswith("/publication.json"):
                continue
            publication = store.read_artifact(key)
            if (
                not isinstance(publication, dict)
                or publication.get("status") != "ready"
                or publication.get("source_sha256") != source_hash
            ):
                continue
            root = key.rsplit("/", 1)[0]
            ready = store.read_artifact(publication.get("result_ref", f"{root}/ready.json"))
            if not isinstance(ready, dict):
                raise PrecisionError("A ready precision publication has a missing result record")
            payload = ready.get("payload")
            indices = publication.get("segment_indices")
            if (
                not isinstance(payload, dict)
                or not isinstance(indices, list)
                or any(type(index) is not int or index not in segments for index in indices)
                or ready.get("stage") not in {"ready", "result"}
                or ready.get("fingerprint") != publication.get("fingerprint")
                or ready.get("fingerprint") != root.rsplit("/", 1)[-1]
                or ready.get("payload_hash") != identity(payload)
            ):
                raise PrecisionError("A ready precision publication has a corrupt result record")
            current = [segments[index] for index in indices]
            if "targets_ref" in payload:
                archive = PrecisionArchive(store)
                targets, draft = (
                    archive.get(payload["targets_ref"]),
                    archive.get(payload["draft_ref"]),
                )
                sources_match = [
                    identity(segment.source) for segment in current
                ] == publication.get("source_hashes")
            else:
                binding = store.read_artifact(f"{root}/inputs.json")
                if not isinstance(binding, dict) or not isinstance(binding.get("plan"), dict):
                    continue
                targets, draft = payload.get("targets"), payload.get("draft")
                sources_match = [segment.source for segment in current] == binding["plan"].get(
                    "sources"
                )
            if (
                [segment.target for segment in current] != targets
                or [segment.target_before_polish for segment in current] != draft
                or not sources_match
                or publication.get("target_hash") != identity(targets)
            ):
                continue
            store.write_artifact(key, {**publication, "status": "published"})
            store.log_event(
                "precision_publication_recovered", chapter=chapter.index, checkpoint=root
            )


class _PrecisionRun:
    def __init__(
        self,
        client: LLMClient,
        config: Config,
        plan: BatchPlan,
        store: Storage,
        *,
        checkpoint: Callable[[], None] | None,
        progress: Callable[[str], None] | None,
    ):
        self.client = client
        self.config = config
        self.plan = plan
        self.store = store
        self.checkpoint = checkpoint
        self.progress = progress
        self.inputs = PrecisionInputs(
            sources=plan.sources,
            terms=plan.terms,
            context=plan.context,
            style=plan.style,
            book_synopsis=plan.book_synopsis,
            chapter_digest=plan.chapter_digest,
            annotation_contexts=plan.annotation_contexts,
            next_source=plan.next_source,
            allow_empty_translations=plan.allow_empty_translations,
        )
        source_hash = store.load_manifest().get("source_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise PrecisionError("Precision checkpoints require the source content hash")
        self.binding = {
            "version": _VERSION,
            "source_sha256": source_hash,
            "plan": asdict(plan),
            "policy": config.language_policy("translation").task_fingerprint("precision"),
            "glossary_reading_source": "ja",
            "inference": inference_snapshot(config.llm, _OPERATIONS),
        }
        self.fingerprint = identity(self.binding)
        self.key = (
            f"precision/chapters/{plan.chapter}/"
            f"{plan.start_index}-{len(plan.sources)}/{self.fingerprint}"
        )
        self.archive = PrecisionArchive(store, source_lang=config.source_lang)
        self.records: PrecisionRecords | None = None

    def agent(self, stage: str) -> PrecisionAgent:
        if self.records is None:
            raise PrecisionError("Precision replay metadata was not prepared")
        records = self.records
        return PrecisionAgent(
            self.client,
            self.config,
            recorder=lambda op, messages, invoke, validate: records.record(
                stage, op, messages, invoke, validate
            ),
        )

    def execute(self) -> BatchResult:
        saved = self.read("result")
        if saved is not None:
            result = self.result(saved)
            self.prepare_publication(result)
            PrecisionBatchExecutor.prepared_publication(self.store, result)
            return result
        # Compatible paid outputs remain reusable within this semantic fingerprint.
        legacy = self.read("ready") or self.read("synthesis")
        self.prepare_metadata(legacy is not None)
        if legacy is not None:
            saved = self.references(legacy["targets"], legacy["draft"], None, legacy=True)
            self.write("result", saved)
            result = self.result(saved)
            self.prepare_publication(result)
            return result
        self.store.log_event(
            "precision_batch_started",
            chapter=self.plan.chapter,
            start_index=self.plan.start_index,
            count=len(self.plan.sources),
            checkpoint=self.key,
        )
        try:
            if any(
                any(character.isalpha() for character in source) for source in self.plan.sources
            ):
                drafts = self.generate()
                with self.store.state_lock():
                    recovered = self.records.completed("synthesis") if self.records else None
                if recovered is None:
                    self.notify("synthesis", "Combining and polishing three drafts")
                    agent = self.agent("synthesis")
                    targets = self.targets(agent.synthesize(self.inputs, drafts))
                    call_ref = agent.last_call_ref
                else:
                    targets, call_ref = recovered
                payload = self.references(self.targets(targets), drafts[0], call_ref)
            else:
                payload = self.references(list(self.plan.sources), list(self.plan.sources), None)
            result = self.result(payload)
            self.write("result", payload)
            self.prepare_publication(result)
            return result
        except Exception as error:
            self.store.log_event(
                "precision_batch_paused",
                chapter=self.plan.chapter,
                start_index=self.plan.start_index,
                checkpoint=self.key,
                error_type=type(error).__name__,
            )
            raise

    def result(self, payload: dict[str, Any]) -> BatchResult:
        with self.store.state_lock():
            return BatchResult(
                tuple(self.targets(self.archive.get(payload["targets_ref"]))),
                tuple(self.targets(self.archive.get(payload["draft_ref"]))),
                precision_key=self.key,
            )

    def prepare_metadata(self, legacy: bool) -> None:
        with self.store.state_lock():
            plan_refs = self.archive.freeze_plan(asdict(self.plan))
            meta = {
                "schema": 2,
                "fingerprint": self.fingerprint,
                "source_sha256": self.binding["source_sha256"],
                "policy": self.binding["policy"],
                "glossary_reading_source": self.binding["glossary_reading_source"],
                "inference": self.binding["inference"],
                "plan": plan_refs,
                "legacy_unrecorded_requests": legacy,
            }
            self.store.write_artifact(f"{self.key}/meta.json", meta)
        self.records = PrecisionRecords(
            self.store,
            self.archive,
            self.key,
            self.fingerprint,
            plan_refs["glossary_ref"],
            self.client,
            self.config.llm.model_dump(mode="json"),
        )

    def references(
        self, targets: list[str], draft: list[str], call_ref: str | None, *, legacy: bool = False
    ) -> dict[str, Any]:
        with self.store.state_lock():
            return {
                "targets_ref": self.archive.put(targets),
                "draft_ref": self.archive.put(draft),
                "call_ref": call_ref,
                "legacy_unrecorded_request": legacy,
            }

    def prepare_publication(self, result: BatchResult) -> None:
        key = f"{self.key}/publication.json"
        existing = self.store.read_artifact(key)
        if existing is not None:
            PrecisionBatchExecutor.prepared_publication(self.store, result)
            if existing.get("result_ref") == f"{self.key}/result.json":
                return
        self.store.write_artifact(
            key,
            {
                **(existing or {}),
                "status": (existing or {}).get("status", "ready"),
                "fingerprint": self.fingerprint,
                "source_sha256": self.binding["source_sha256"],
                "segment_indices": list(self.plan.segment_indices),
                "expected_targets": [None] * len(result.targets),
                "target_hash": identity(result.targets),
                "source_hashes": [identity(source) for source in self.plan.sources],
                "result_ref": f"{self.key}/result.json",
            },
        )

    def targets(self, value: Any) -> list[str]:
        if (
            not isinstance(value, list)
            or len(value) != len(self.plan.sources)
            or any(not isinstance(text, str) for text in value)
        ):
            raise PrecisionError("Invalid precision output structure")
        output = [strip_ruby_markers(text) for text in value]
        for source, target in zip(self.plan.sources, output):
            if not any(character.isalpha() for character in source):
                if target != source:
                    raise PrecisionError("A precision output changed a protected source segment")
            elif not self.plan.allow_empty_translations and not target.strip():
                raise PrecisionError("A precision output contains an empty translation")
        return output

    def read(self, stage: str) -> dict[str, Any] | None:
        record = self.store.read_artifact(f"{self.key}/{stage}.json")
        if record is None:
            return None
        if (
            not isinstance(record, dict)
            or record.get("fingerprint") != self.fingerprint
            or record.get("stage") != stage
            or not isinstance(record.get("payload"), dict)
            or record.get("payload_hash") != identity(record["payload"])
        ):
            raise PrecisionError("Invalid precision checkpoint; its saved content was changed")
        return record["payload"]

    def write(self, stage: str, payload: dict[str, Any]) -> None:
        self.store.write_artifact(
            f"{self.key}/{stage}.json",
            {
                "fingerprint": self.fingerprint,
                "stage": stage,
                "payload_hash": identity(payload),
                "payload": payload,
            },
        )
        if self.checkpoint:
            self.checkpoint()

    def notify(self, stage: str, label: str) -> None:
        if self.progress:
            self.progress(label)
        self.store.log_event(
            "precision_stage",
            chapter=self.plan.chapter,
            start_index=self.plan.start_index,
            stage=stage,
            checkpoint=self.key,
        )

    def generate(self) -> list[list[str]]:
        candidates: dict[str, list[str]] = {}
        pending: list[str] = []
        for candidate in _DRAFT_IDS:
            cached = self.read(f"drafts/{candidate}")
            if cached is None:
                with self.store.state_lock():
                    recovered = (
                        self.records.completed(f"drafts/{candidate}") if self.records else None
                    )
                if recovered is None:
                    pending.append(candidate)
                else:
                    targets, call_ref = recovered
                    with self.store.state_lock():
                        reference = self.archive.put(self.targets(targets))
                    self.write(
                        f"drafts/{candidate}", {"targets_ref": reference, "call_ref": call_ref}
                    )
                    candidates[candidate] = self.targets(targets)
            else:
                with self.store.state_lock():
                    candidates[candidate] = self.targets(
                        self.archive.get(cached["targets_ref"])
                        if "targets_ref" in cached
                        else cached.get("targets")
                    )
                    if "targets_ref" not in cached:
                        meta_key = f"{self.key}/meta.json"
                        meta = self.store.read_artifact(meta_key)
                        if isinstance(meta, dict):
                            self.store.write_artifact(
                                meta_key, {**meta, "legacy_unrecorded_requests": True}
                            )
        if pending:
            self.notify("drafts", "Generating three translations")

            def compute(candidate: str) -> dict[str, Any]:
                agent = self.agent(f"drafts/{candidate}")
                targets = self.targets(agent.translate(self.inputs))
                with self.store.state_lock():
                    return {
                        "targets_ref": self.archive.put(targets),
                        "call_ref": agent.last_call_ref,
                    }

            failures: list[Exception] = []
            with (
                self.client.interrupt_scope(),
                ThreadPoolExecutor(max_workers=len(_DRAFT_IDS)) as pool,
            ):
                futures = {pool.submit(compute, candidate): candidate for candidate in pending}
                for future in as_completed(futures):
                    candidate = futures[future]
                    try:
                        payload = future.result()
                    except Exception as error:
                        failures.append(error)
                        continue
                    try:
                        self.write(f"drafts/{candidate}", payload)
                    except Exception as error:
                        # Retain completed siblings before propagating an orderly interruption.
                        failures.append(error)
                    with self.store.state_lock():
                        candidates[candidate] = self.targets(
                            self.archive.get(payload["targets_ref"])
                        )
            if failures:
                raise failures[0]
        return [candidates[candidate] for candidate in _DRAFT_IDS]
