"""Replay-ready per-call records, separate from precision resume/publication decisions."""

from __future__ import annotations

import logging
import platform
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from time import perf_counter
from typing import Any
from uuid import uuid4

from ..llm.base import LLMClient, Messages
from ..llm.errors import describe_provider_failure
from ..llm.routing import identity
from ..storage.precision_archive import PrecisionArchive, safe_model_snapshot
from ..storage.protocol import Storage

_LOGGER = logging.getLogger(__name__)


def _versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {"python": platform.python_version()}
    for name in ("wenyi-core", "openai", "google-genai", "tiktoken"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


class PrecisionRecords:
    """Persist inputs before calling, then record output even if local validation fails."""

    def __init__(
        self,
        store: Storage,
        archive: PrecisionArchive,
        key: str,
        fingerprint: str,
        glossary_ref: str,
        client: LLMClient,
        declared_models: dict[str, Any],
    ):
        self.store, self.archive, self.key = store, archive, key
        self.fingerprint, self.glossary_ref, self.client = fingerprint, glossary_ref, client
        with store.state_lock():
            self.environment_ref = archive.put(_versions())
            self.declared_models_ref = archive.put(safe_model_snapshot(declared_models))

    def record(
        self,
        stage: str,
        operation: str,
        messages: Messages,
        invoke: Callable[[], str],
        validate: Callable[[str], list[str]],
    ) -> tuple[list[str], str]:
        key = f"{self.key}/calls/{stage}/{uuid4().hex}.json"
        with self.store.state_lock():
            snapshot = self.client.request_snapshot(operation)
            model_ref = (
                self.archive.put(safe_model_snapshot(snapshot)) if snapshot is not None else None
            )
            record: dict[str, Any] = {
                "schema": 1,
                "fingerprint": self.fingerprint,
                "stage": stage,
                "operation": operation,
                "status": "started",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "environment_ref": self.environment_ref,
                "declared_models_ref": self.declared_models_ref,
                "effective_model_ref": model_ref,
                "effective_model_known": model_ref is not None,
                "messages": self.archive.messages(messages, self.glossary_ref),
                "json_mode": True,
                "max_tokens": None,
            }
            self.save(key, record)
        events: list[dict[str, Any]] = []
        start = perf_counter()

        def capture(event: str, **data: Any) -> None:
            events.append(
                {
                    "event": event,
                    "elapsed_ms": round((perf_counter() - start) * 1000, 3),
                    **deepcopy(data),
                }
            )

        raw: str | None = None
        phase = "request"
        try:
            with self.client.capture_events(capture):
                raw = invoke()
            record["model_elapsed_ms"] = round((perf_counter() - start) * 1000, 3)
            phase = "archive_response"
            # Save the received text before validation so malformed outputs remain diagnosable.
            with self.store.state_lock():
                record["response"] = self.archive.messages(
                    [{"role": "assistant", "content": raw}], self.glossary_ref
                )
                record["status"] = "received"
                record["events"] = events
                record["usage_known"] = any(event["event"] == "llm_usage" for event in events)
                self.save(key, record)
            phase = "validate"
            targets = validate(raw)
            phase = "archive_result"
            with self.store.state_lock():
                record["targets_ref"] = self.archive.put(targets)
                record["status"] = "completed"
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                self.save(key, record)
            return targets, key
        except BaseException as error:
            record["model_elapsed_ms"] = record.get(
                "model_elapsed_ms", round((perf_counter() - start) * 1000, 3)
            )
            record["events"] = events
            record["usage_known"] = any(event["event"] == "llm_usage" for event in events)
            record["status"] = "interrupted" if not isinstance(error, Exception) else "failed"
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            record["error_type"] = type(error).__name__
            if phase == "validate":
                record["error_category"] = "invalid_output"
                record["error_message"] = "The received model output failed local validation."
            elif phase.startswith("archive_"):
                record["error_category"] = "archive_storage_failed"
                record["error_message"] = "The model record could not be persisted."
            else:
                record.update(describe_provider_failure(error).log_fields())
            try:
                with self.store.state_lock():
                    self.save(key, record)
            except Exception as storage_error:
                # Log only the type; failed persistence must stop further paid attempts.
                _LOGGER.error(
                    "Could not persist precision call failure: %s", type(storage_error).__name__
                )
                if isinstance(error, Exception):
                    raise
                # Keep cancellation and process-interruption semantics even if recording fails.
            raise

    def save(self, key: str, record: dict[str, Any]) -> None:
        self.store.write_artifact(key, {**record, "record_hash": identity(record)})

    def completed(self, stage: str) -> tuple[list[str], str] | None:
        """Recover paid outputs whose stage binding was interrupted after receipt completion."""
        completed: list[tuple[str, str, list[str]]] = []
        for key in self.store.list_artifacts(f"{self.key}/calls/{stage}/"):
            record = self.store.read_artifact(key)
            if not isinstance(record, dict):
                raise ValueError("Invalid precision call receipt")
            payload = {name: value for name, value in record.items() if name != "record_hash"}
            if record.get("record_hash") != identity(payload):
                raise ValueError("Precision call receipt content was changed")
            if record.get("fingerprint") != self.fingerprint or record.get("stage") != stage:
                raise ValueError("Precision call receipt does not match its stage")
            if record.get("status") != "completed":
                continue
            restored = load_precision_call(self.store, key)
            targets = restored.get("targets")
            if not isinstance(targets, list) or any(not isinstance(text, str) for text in targets):
                raise ValueError("Invalid completed precision output")
            completed.append((record["finished_at"], key, targets))
        if not completed:
            return None
        _time, key, targets = max(completed, key=lambda item: (item[0], item[1]))
        return targets, key


def load_precision_call(store: Storage, key: str) -> dict[str, Any]:
    """Restore one historical request without loading current templates or invoking a model."""
    record = store.read_artifact(key)
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise ValueError("Invalid or missing precision call record")
    payload = {name: value for name, value in record.items() if name != "record_hash"}
    if record.get("record_hash") != identity(payload):
        raise ValueError("Precision call record content was changed")
    archive = PrecisionArchive(store)
    result = dict(record)
    result["messages"] = archive.load_messages(record["messages"])
    result["declared_models"] = archive.get(record["declared_models_ref"])
    result["effective_model"] = (
        archive.get(record["effective_model_ref"]) if record.get("effective_model_ref") else None
    )
    result["environment"] = archive.get(record["environment_ref"])
    if "response" in record:
        result["raw_response"] = archive.load_messages(record["response"])[0]["content"]
    if "targets_ref" in record:
        result["targets"] = archive.get(record["targets_ref"])
    return result
