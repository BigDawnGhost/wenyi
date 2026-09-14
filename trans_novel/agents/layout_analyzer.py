"""使用模型对原始排版证据做严格的角色分类。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from trans_novel.agents.base import Agent, WorkflowProtocolError, retry_protocol


class LayoutObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: str
    role: str | None
    level: int | None


class _LayoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observations: list[LayoutObservation]


_SYSTEM_PROMPT = """You classify ORIGINAL book layout evidence, never translated output.
Return a JSON object exactly {"observations":[{"node_id":string,"role":string|null,"level":integer|null}]}.
Return every requested node_id exactly once and no other IDs. Use only an allowed role.
Use role null when evidence is unknown or conflicting. level is required from 1 through 6 only
for heading; every other role and null must have level null. Class names are book-local grouping
evidence, never universal semantic rules. Consider all supplied markup, native semantics,
adjacent context, reference relationships, and matched ORIGINAL CSS."""


class LayoutAnalyzer(Agent):
    def classify(
        self,
        *,
        samples: list[dict[str, Any]],
        allowed_roles: tuple[str, ...],
    ) -> list[LayoutObservation]:
        if not samples:
            return []
        requested: list[str] = []
        for sample in samples:
            node_id = sample.get("node_id") if isinstance(sample, dict) else None
            if not isinstance(node_id, str) or not node_id or node_id in requested:
                raise ValueError("samples require unique non-empty node_id values")
            requested.append(node_id)
        if (
            not allowed_roles
            or len(set(allowed_roles)) != len(allowed_roles)
            or any(not isinstance(role, str) or not role for role in allowed_roles)
        ):
            raise ValueError("allowed_roles must contain unique non-empty strings")
        user = json.dumps(
            {"allowed_roles": list(allowed_roles), "samples": samples},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

        def call() -> list[LayoutObservation]:
            data = self._ask_json(
                _SYSTEM_PROMPT,
                user,
                agent="analyst",
                operation="layout.classify",
                strict=True,
            )
            try:
                response = _LayoutResponse.model_validate(data)
            except ValidationError as error:
                raise WorkflowProtocolError("layout_classification_invalid") from error
            observations = response.observations
            observed_ids = [item.node_id for item in observations]
            if len(observed_ids) != len(set(observed_ids)):
                raise WorkflowProtocolError("layout_classification_duplicate_id")
            if set(observed_ids) != set(requested) or len(observed_ids) != len(requested):
                raise WorkflowProtocolError("layout_classification_id_mismatch")
            allowed = set(allowed_roles)
            for item in observations:
                if item.role is not None and item.role not in allowed:
                    raise WorkflowProtocolError("layout_classification_role_invalid")
                if item.role == "heading":
                    if (
                        isinstance(item.level, bool)
                        or not isinstance(item.level, int)
                        or not 1 <= item.level <= 6
                    ):
                        raise WorkflowProtocolError("layout_classification_level_invalid")
                elif item.level is not None:
                    raise WorkflowProtocolError("layout_classification_level_invalid")
            by_id = {item.node_id: item for item in observations}
            return [by_id[node_id] for node_id in requested]

        return retry_protocol(call, retries=self.config.pipeline.protocol_retry_limit)


__all__ = ["LayoutAnalyzer", "LayoutObservation"]
