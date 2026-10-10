"""Source-grounded terminology discovery, evidence extraction and bounded merging."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..glossary.evidence import trusted_match_sources
from ..glossary.evidence_models import (
    DiscoveryRejection,
    DiscoveryResult,
    KnowledgeResult,
    SourcePassage,
)
from ..glossary.store import GlossaryTerm, _match_text, source_matches_text
from ..llm.json_parser import JsonParseError
from .base import Agent

# Only bounded prose crosses recursive merge boundaries; ledgers stay local.
MERGE_INPUT_BUDGET = 24000
NOTE_BUDGET = 4000
KNOWLEDGE_FIELD_BUDGET = 256
_CITATION_RULES = (
    "Copy exact passage reference values (p1, p2, ...) into evidence_refs. "
    "These labels belong only to this request and each names one supplied source slice. "
    "source_reference is its stable audit ID and may also be copied exactly. "
    "Never shorten IDs, infer offsets, reconstruct chapter IDs, or cite a different request."
)


class TerminologyReferenceError(ValueError):
    """Report reference shape/counts without exposing model prose or untrusted IDs."""

    def __init__(
        self,
        *,
        operation: str,
        reason: Literal["missing", "outside_input"],
        field: str,
        allowed_count: int,
        received_count: int,
        unknown_count: int = 0,
    ):
        self.operation = operation
        self.reason = reason
        self.field = field
        self.allowed_count = allowed_count
        self.received_count = received_count
        self.unknown_count = unknown_count
        detail = "are missing" if reason == "missing" else "are outside the supplied input"
        super().__init__(
            f"Terminology evidence references {detail} "
            f"(operation={operation}, field={field}, allowed={allowed_count}, "
            f"received={received_count}, unknown={unknown_count})"
        )

    def diagnostic(self) -> dict[str, str | int]:
        """Return only fixed operation/field identities and aggregate counts."""
        return {
            "operation": self.operation,
            "reason": self.reason,
            "field": self.field,
            "allowed_count": self.allowed_count,
            "received_count": self.received_count,
            "unknown_count": self.unknown_count,
        }


def _knowledge_field(location: tuple[str | int, ...]) -> str:
    """Describe a schema path without copying arbitrary model-generated property names."""
    if not location:
        return "$"
    name, *rest = location
    if name not in KnowledgeResult.model_fields:
        return "<extra>"
    field = str(name)
    if name in {"aliases", "evidence_refs"} and rest:
        if isinstance(rest[0], int):
            field += f"[{rest.pop(0)}]"
    return field + (".<extra>" if rest else "")


class TerminologyKnowledgeError(ValueError):
    """Expose bounded schema diagnostics, never invalid values or extra property names."""

    def __init__(self, error: ValidationError, *, operation: str):
        self.operation = operation
        self.issue_count = error.error_count()
        self.issues: list[dict[str, str]] = []
        safe_codes = {
            "bool_type",
            "extra_forbidden",
            "list_type",
            "literal_error",
            "missing",
            "model_type",
            "string_too_long",
            "string_too_short",
            "string_type",
            "too_short",
        }
        for problem in error.errors(include_input=False, include_context=False, include_url=False):
            code = problem["type"] if problem["type"] in safe_codes else "invalid_value"
            issue = {"field": _knowledge_field(problem["loc"]), "code": code}
            if issue not in self.issues:
                self.issues.append(issue)
            if len(self.issues) == 8:
                break
        fields = ", ".join(f"{issue['field']}:{issue['code']}" for issue in self.issues)
        super().__init__(
            "Invalid terminology knowledge fields "
            f"(operation={operation}, errors={self.issue_count}, fields={fields})"
        )

    def diagnostic(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "reason": "invalid_fields",
            "issue_count": self.issue_count,
            "issues": self.issues,
        }


class _BoundedKnowledgeResult(KnowledgeResult):
    note: str = Field(default="", max_length=NOTE_BUDGET)
    reading: str = Field(default="", max_length=KNOWLEDGE_FIELD_BUDGET)
    gender: str = Field(default="", max_length=KNOWLEDGE_FIELD_BUDGET)


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: str = "term"
    evidence_refs: list[str] = Field(min_length=1)


def _refs(
    refs: list[str],
    allowed: set[str],
    *,
    operation: str,
    field: str = "evidence_refs",
    required: bool = False,
) -> None:
    if (required and not refs) or any(ref not in allowed for ref in refs):
        raise TerminologyReferenceError(
            operation=operation,
            reason="missing" if not refs else "outside_input",
            field=field,
            allowed_count=len(allowed),
            received_count=len(refs),
            unknown_count=sum(ref not in allowed for ref in refs),
        )


def _missing_reference_error(
    error: ValidationError,
    allowed: set[str],
    operation: str,
    field: str = "evidence_refs",
) -> TerminologyReferenceError | None:
    """Classify schema-level empty citations without retaining invalid response values."""
    for problem in error.errors(include_input=False, include_context=False, include_url=False):
        if problem["type"] not in {"missing", "too_short"}:
            continue
        location = problem["loc"]
        if location == ("evidence_refs",):
            missing_field = field
        else:
            continue
        return TerminologyReferenceError(
            operation=operation,
            reason="missing",
            field=missing_field,
            allowed_count=len(allowed),
            received_count=0,
        )
    return None


def _knowledge(
    data: Any,
    allowed: set[str],
    *,
    operation: str,
) -> KnowledgeResult:
    try:
        validated = _BoundedKnowledgeResult.model_validate(data, strict=True)
        result = KnowledgeResult.model_validate(validated.model_dump(), strict=True)
    except ValidationError as error:
        reference_error = _missing_reference_error(error, allowed, operation)
        if reference_error is not None:
            raise reference_error from None
        raise TerminologyKnowledgeError(error, operation=operation) from None
    _refs(
        result.evidence_refs,
        allowed,
        operation=operation,
        required=bool(result.note.strip() or result.reading or result.gender or result.aliases),
    )
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _citation_passages(
    passages: list[SourcePassage],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Supply a closed local-label table without changing stable source identities."""
    canonical = {passage.reference for passage in passages}
    labels = [f"p{index}" for index in range(1, len(passages) + 1)]
    if "" in canonical or len(canonical) != len(passages):
        raise ValueError("Empty or duplicate terminology citation identifiers")
    if canonical.intersection(labels):
        raise ValueError("Ambiguous terminology citation identifiers")
    wire = [
        {**passage.model_dump(), "reference": label, "source_reference": passage.reference}
        for label, passage in zip(labels, passages)
    ]
    return wire, {label: passage.reference for label, passage in zip(labels, passages)}


def _canonical_citations(data: Any, labels: dict[str, str]) -> Any:
    """Decode only explicit table entries; leave unknown IDs for strict validation."""
    if not isinstance(data, dict):
        return data
    result = dict(data)
    references = data.get("evidence_refs")
    if isinstance(references, list):
        result["evidence_refs"] = [
            labels.get(reference, reference) if isinstance(reference, str) else reference
            for reference in references
        ]
    return result


class TerminologyAgent(Agent):
    policy_phase = "analysis"

    def _request(self, task: str, payload: dict[str, Any]) -> Any:
        if task in {"evidence", "merge"}:
            schema = _BoundedKnowledgeResult.model_json_schema()
            if task == "merge":
                schema["properties"]["aliases"]["const"] = []
            payload = {
                **payload,
                "response_schema": schema,
                "response_rules": (
                    "Return one JSON object matching response_schema, without a wrapper or "
                    "additional properties. Use strings for note/reading/gender and arrays "
                    "for aliases/evidence_refs. Use an empty string or array "
                    "for absent values, never null. Do not copy input-only term or reference "
                    "properties into the response."
                ),
            }
        if task == "merge" and len(_json(payload)) > MERGE_INPUT_BUDGET:
            raise ValueError("Terminology merge input exceeds budget; group results before merging")
        try:
            return self._ask_json(
                self.render(f"terminology_{task}_system"),
                self.render(
                    f"terminology_{task}_user",
                    payload=_json(payload),
                ),
                operation=f"terminology.{task}",
            )
        except JsonParseError:
            raise ValueError("Invalid terminology JSON response") from None

    def discover(
        self,
        passages: list[SourcePassage],
        existing: list[GlossaryTerm],
    ) -> DiscoveryResult:
        """Keep source-attested proposals; audit invalid items without aborting the batch."""
        if not passages:
            return DiscoveryResult()
        by_ref = {p.reference: p for p in passages}
        allowed = set(by_ref)
        wire, citations = _citation_passages(passages)
        data = self._request(
            "discover",
            {
                "passages": wire,
                "citation_rules": _CITATION_RULES,
                "existing": [asdict(t) for t in existing],
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
            raise ValueError("Invalid terminology discovery fields")
        terms: list[GlossaryTerm] = []
        rejections: list[DiscoveryRejection] = []
        seen: set[str] = set()
        for index, raw in enumerate(data["terms"]):
            raw = _canonical_citations(raw, citations)
            try:
                candidate = _Candidate.model_validate(raw)
            except ValidationError as error:
                reference_error = _missing_reference_error(
                    error, allowed, "terminology.discover", f"terms[{index}].evidence_refs"
                )
                rejections.append(
                    DiscoveryRejection(
                        collection="terms",
                        index=index,
                        reason=reference_error.reason if reference_error else "invalid_fields",
                    )
                )
                continue
            try:
                _refs(
                    candidate.evidence_refs,
                    allowed,
                    operation="terminology.discover",
                    field=f"terms[{index}].evidence_refs",
                    required=True,
                )
            except TerminologyReferenceError as error:
                rejections.append(
                    DiscoveryRejection(collection="terms", index=index, reason=error.reason)
                )
                continue
            if not any(
                source_matches_text(candidate.source, by_ref[ref].source)
                for ref in candidate.evidence_refs
            ):
                rejections.append(
                    DiscoveryRejection(collection="terms", index=index, reason="source_absent")
                )
                continue
            identity = _match_text(candidate.source).strip()
            matches = [
                term
                for term in existing
                if any(
                    _match_text(key).strip() == identity
                    for key in trusted_match_sources(term, existing)
                )
            ]
            term = (
                replace(matches[0], aliases=list(matches[0].aliases))
                if len(matches) == 1
                else GlossaryTerm(
                    source=candidate.source,
                    target=candidate.target,
                    type=candidate.type,
                    first_chapter=by_ref[candidate.evidence_refs[0]].chapter,
                )
            )
            identity = _match_text(term.source).strip()
            if identity not in seen:
                terms.append(term)
                seen.add(identity)
        return DiscoveryResult(terms=terms, rejections=rejections)

    def enrich(self, term: GlossaryTerm, passages: list[SourcePassage]) -> KnowledgeResult:
        """Extract one caller-bounded evidence group; empty evidence is not completion."""
        if not passages:
            return KnowledgeResult()
        wire, citations = _citation_passages(passages)
        data = self._request(
            "evidence",
            {"term": asdict(term), "passages": wire, "citation_rules": _CITATION_RULES},
        )
        data = _canonical_citations(data, citations)
        by_ref = {p.reference: p for p in passages}
        result = _knowledge(data, set(by_ref), operation="terminology.evidence")
        result.evidence_refs = list(dict.fromkeys(result.evidence_refs))
        if any(
            not any(source_matches_text(alias, by_ref[ref].source) for ref in result.evidence_refs)
            for alias in result.aliases
        ):
            raise ValueError("Terminology alias source is absent from cited passages")
        return result

    def merge(self, term: GlossaryTerm, results: list[KnowledgeResult]) -> KnowledgeResult:
        """Merge one bounded group; callers recursively group larger collections.

        The model sees bounded prose and ephemeral group references, never the
        growing evidence ledger. References and aliases are unioned locally in
        first-seen order.
        Oversized prose is rejected, not truncated or reported as complete.
        """
        if not results:
            return KnowledgeResult()
        allowed = {ref for result in results for ref in result.evidence_refs}
        normalized = [
            _knowledge(
                r.model_dump(),
                allowed,
                operation="terminology.merge",
            )
            for r in results
        ]
        groups = [
            {
                "reference": f"group:{index}",
                "note": result.note,
                "reading": result.reading,
                "gender": result.gender,
            }
            for index, result in enumerate(normalized)
        ]
        payload = {
            "term": {"source": term.source, "target": term.target, "type": term.type},
            "results": groups,
        }
        merged = _knowledge(
            self._request("merge", payload),
            {group["reference"] for group in groups},
            operation="terminology.merge",
        )
        if merged.aliases:
            raise ValueError("Terminology merge introduced an unsupported alias")
        if any(result.note.strip() for result in normalized) and not merged.note.strip():
            raise ValueError("Terminology merge omitted the supplied knowledge note")
        merged.aliases = list(dict.fromkeys(alias for r in normalized for alias in r.aliases))
        merged.evidence_refs = list(
            dict.fromkeys(ref for result in results for ref in result.evidence_refs)
        )
        return merged
