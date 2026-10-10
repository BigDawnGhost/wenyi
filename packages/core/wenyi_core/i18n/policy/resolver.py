"""Resolve role-separated bindings and effective resources into immutable plans."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, replace
from string import Template
from typing import Any

from ..languages import honorific_rule, profile, require_language
from ..resources import read_json, read_text
from .models import (
    ExportOptions,
    OperationBinding,
    OperationSpec,
    PairPolicy,
    PolicyContext,
    PolicyPlan,
    ResolvedOperation,
    content_hash,
)
from .registry import OPERATIONS, validate_bindings, validate_options

# Every consumed template is frozen with the invocation; unused languages are never hashed.
TASK_GROUPS = {
    "analysis": ("analyzer", "chapter_digest", "book_synopsis"),
    "translation": (
        "translator",
        "polisher",
        "title_translator",
        "glossary_extractor",
        "glossary_history",
        "annotation_aligner",
    ),
    "review": ("reviewer", "review_agent", "review_arbiter", "review_fixer"),
    "srt": ("srt_batch", "srt_single"),
}


def sort_operations(
    selected: tuple[OperationSpec, ...],
    registry: Mapping[str, OperationSpec],
) -> tuple[OperationSpec, ...]:
    """Stable same-point topological order with explicit dependency/conflict validation."""
    specs = {spec.id: spec for spec in selected}
    edges = {key: set() for key in specs}
    concerns: dict[tuple[str, str], str] = {}
    for spec in selected:
        for concern in (*spec.writes, spec.exclusive_group):
            if not concern:
                continue
            key = (spec.point, concern)
            if key in concerns and concerns[key] != spec.id:
                raise ValueError(f"Conflicting language operations: {concerns[key]} and {spec.id}")
            concerns[key] = spec.id
        for dependency in spec.requires:
            if dependency not in specs:
                raise ValueError(f"{spec.id} requires selected operation {dependency}")
        for other, before in [(key, False) for key in spec.after + spec.requires] + [
            (key, True) for key in spec.before
        ]:
            if other not in registry:
                raise ValueError(f"Unknown ordering reference {other} in {spec.id}")
            if other not in specs:
                continue
            if specs[other].point != spec.point:
                raise ValueError(f"Ordering crosses an extension point: {spec.id}, {other}")
            edges[other if before else spec.id].add(spec.id if before else other)
    result = []
    while edges:
        ready = sorted(key for key, dependencies in edges.items() if not dependencies)
        if not ready:
            raise ValueError("Language operation ordering cycle: " + ", ".join(sorted(edges)))
        for key in ready:
            result.append(specs[key])
            del edges[key]
        for dependencies in edges.values():
            dependencies.difference_update(ready)
    return tuple(result)


def _rules(context: PolicyContext) -> dict[str, str]:
    common = read_json("shared/guidance.json")
    target = profile(context.target)
    source = profile(context.source) if context.source != "auto" else {}
    source_guidance = source.get("source_guidance", common["source"])
    target_guidance = target["target_guidance"]
    default_guidance = "\n".join(
        (
            source_guidance,
            target_guidance,
            common["evidence"],
            honorific_rule("keep_style", context.source, context.target),
        )
    )
    return {
        "src_label": source.get("label", "the source language"),
        "source_language": source.get("label", "the source language"),
        "tgt_label": target["label"],
        "target_language": target["english_name"],
        "lang_guidance": default_guidance,
        "configured_lang_guidance": "\n".join(
            (
                source_guidance,
                target_guidance,
                common["evidence"],
                honorific_rule(context.honorific_strategy, context.source, context.target),
            )
        ),
        "target_guidance": target_guidance,
        "term_guidance": source.get("term_guidance", common["reading"]) + common["evidence"],
        "punct_rule": target["punctuation_rule"],
        "title_rule": target["title_rule"],
        "digest_length": target["digest_length"],
        "synopsis_length": target["synopsis_length"],
        "review_evidence_tools": read_text("shared/review_evidence_tools.txt"),
        "metadata_guidance": Template(read_text("shared/metadata_guidance.txt")).substitute(
            tgt_label=target["english_name"]
        ),
    }


def _template_path(task: str, *, terminology: bool) -> str:
    """Resolve selected variants while retaining the public task names."""
    if terminology and task in {"analyzer_system", "chapter_digest_system", "book_synopsis_system"}:
        return f"tasks/{task.removesuffix('_system')}_terminology_system.txt"
    return f"tasks/{task}.txt"


def _templates(context: PolicyContext, *, terminology: bool) -> tuple[tuple[str, str], ...]:
    if context.phase == "export":
        return ()
    groups = context.task_groups or TASK_GROUPS["srt" if context.path == "srt" else context.phase]
    if not terminology:
        groups = tuple(group for group in groups if not group.startswith("terminology_"))
    if terminology and context.phase in {"analysis", "translation"}:
        groups += ("terminology_discover", "terminology_evidence", "terminology_merge")
    # Explicit filenames keep registry selection independent of filesystem enumeration.
    from .tasks import TASKS

    return tuple(
        (task, read_text(_template_path(task, terminology=terminology)))
        for task in TASKS
        if any(task.startswith(group + "_") for group in groups)
    )


def resolve_policy(
    context: PolicyContext,
    overrides: dict[str, OperationBinding] | None = None,
) -> PolicyPlan:
    """Validate source definitions and resolve choices without model calls or persistent I/O."""
    context = replace(
        context,
        source=require_language(context.source, allow_auto=True),
        target=require_language(context.target),
    )
    if context.phase not in {"analysis", "translation", "review", "export"} or context.path not in {
        "book",
        "srt",
    }:
        raise ValueError("Unsupported language policy phase or workflow")
    if context.order not in {"target_first", "source_first"}:
        raise ValueError("Unsupported bilingual output order")
    overrides = overrides or {}
    validate_bindings(overrides)
    if context.phase == "export" and context.format not in (
        ("srt",) if context.path == "srt" else ("epub", "docx", "html", "txt", "markdown", "pdf")
    ):
        raise ValueError(f"Unsupported language policy export format: {context.format}")
    if context.backend not in {"native", "weasyprint", "fpdf2", "babeldoc"}:
        raise ValueError(f"Unsupported language policy backend: {context.backend}")
    bindings: dict[str, tuple[OperationBinding, tuple[str, ...]]] = {
        "prompt.language_rules": (OperationBinding(), ("common",)),
        "export.language_metadata": (OperationBinding(), ("common",)),
        "terminology.context": (OperationBinding(), ("pipeline.terminology_context",)),
    }
    metadata: dict[str, str] = {"language_tag": context.target, "about_locale": "en"}
    conflicts: set[str] = set()
    for role, code in (("source", context.source), ("target", context.target)):
        if code == "auto":
            continue
        language = profile(code)
        policy = language.get("policy", {})
        role_bindings = {
            key: OperationBinding.model_validate(value)
            for key, value in policy.get(role, {}).items()
        }
        validate_bindings(role_bindings)
        for key, binding in role_bindings.items():
            if key not in OPERATIONS or role not in OPERATIONS[key].roles:
                raise ValueError(f"Invalid {role} language operation: {key}")
            origin = language.get("_policy_origins", {}).get(role, {}).get(key, code)
            origins = bindings.get(key, (binding, ()))[1] + (f"{role}:{origin}",)
            if (
                key in bindings
                and bindings[key][0] != binding
                and any(origin.startswith("source:") for origin in bindings[key][1])
            ):
                conflicts.add(key)
            bindings[key] = (binding, origins)
        if role == "target":
            metadata.update(policy.get("export", {}))
    pair = f"{context.source}__{context.target}"
    pair_registry = read_json("pairs/registry.json")
    pair_policy = PairPolicy.model_validate(
        read_json(f"pairs/{pair_registry[pair]}").get("policy", {}) if pair in pair_registry else {}
    )
    validate_bindings(pair_policy.operations)
    for key, binding in pair_policy.operations.items():
        if key not in OPERATIONS or "pair" not in OPERATIONS[key].roles:
            raise ValueError(f"Invalid pair language operation: {key}")
        bindings[key] = (binding, (f"pair:{pair}",))
        conflicts.discard(key)
    metadata.update(pair_policy.export.model_dump(exclude_unset=True))
    for key, binding in overrides.items():
        bindings[key] = (binding, bindings.get(key, (binding, ()))[1] + ("developer",))
        conflicts.discard(key)
    if conflicts:
        raise ValueError(
            "Conflicting source/target bindings require an explicit override: "
            + ", ".join(sorted(conflicts))
        )
    selections = []
    for key, (binding, origins) in sorted(bindings.items()):
        spec = OPERATIONS[key]
        options = validate_options(spec, binding)
        language = context.source if spec.roles == ("source", "pair") else context.target
        if binding.mode == "on" and (
            context.path not in spec.paths
            or spec.languages
            and language != "auto"
            and language not in spec.languages
        ):
            raise ValueError(f"Language operation {key} is unavailable for this language/workflow")
        if (
            binding.mode == "on"
            and key == "punctuation.zh_cn"
            and not context.punctuation_normalize
        ):
            raise ValueError(
                "Language operation punctuation.zh_cn is unavailable: output.punctuation_normalize is disabled"
            )
        reasons = []
        phase_matches = (
            spec.point.startswith("export.")
            if context.phase == "export"
            else spec.point == "prompt.compose"
            or spec.point == "evidence.prepare"
            and context.phase in {"analysis", "translation"}
        )
        if not phase_matches:
            # An invocation has separate semantic/export plans. Explicit on is validated
            # when its own phase is compiled, never rejected by an unrelated phase.
            continue
        if spec.languages and language not in spec.languages:
            reasons.append("unsupported language")
        if context.path not in spec.paths:
            reasons.append("unsupported workflow")
        if spec.formats and context.format not in spec.formats:
            reasons.append("unsupported format")
        if spec.backends and context.backend not in spec.backends:
            reasons.append("unsupported backend")
        if key == "punctuation.zh_cn" and not context.punctuation_normalize:
            reasons.append("output.punctuation_normalize is disabled")
        if key == "terminology.context" and not context.terminology_context:
            reasons.append("pipeline.terminology_context is disabled")
        if binding.mode == "on" and reasons:
            raise ValueError(f"Language operation {key} is unavailable: {'; '.join(reasons)}")
        enabled = binding.mode != "off" and not reasons
        reason = (
            "selected"
            if enabled
            else "explicitly disabled"
            if binding.mode == "off"
            else "; ".join(reasons)
        )
        selections.append(
            ResolvedOperation(
                key,
                spec.point,
                spec.implementation_version,
                spec.contract_version,
                options,
                origins,
                enabled,
                reason,
                spec.llm_operations,
            )
        )
    ordered = sort_operations(
        tuple(OPERATIONS[op.id] for op in selections if op.enabled), OPERATIONS
    )
    order = {spec.id: i for i, spec in enumerate(ordered)}
    selections.sort(key=lambda op: (not op.enabled, order.get(op.id, 0), op.id))
    enabled_ids = {op.id for op in selections if op.enabled}
    font = next(
        (
            dict(op.options)["font"]
            for op in selections
            if op.id == "docx.chinese_font" and op.enabled
        ),
        None,
    )
    export = ExportOptions(
        metadata["language_tag"],
        metadata["about_locale"],
        font,
        "markup.japanese_ruby" in enabled_ids,
    )
    terminology = "terminology.context" in enabled_ids and context.path == "book"
    templates = _templates(context, terminology=terminology)
    rules = {} if context.phase == "export" else _rules(context)
    used = set(re.findall(r"\$\{?(\w+)", "\n".join(text for _, text in templates)))
    effective_rules = {key: value for key, value in rules.items() if key in used}
    if any(task in {"translator_system", "review_fixer_system"} for task, _ in templates):
        effective_rules["configured_lang_guidance"] = rules["configured_lang_guidance"]
    resources = tuple(
        (_template_path(task, terminology=terminology), content_hash(text))
        for task, text in templates
    )
    if context.phase == "export" and context.format == "epub" and context.about_page:
        resources += (
            (
                f"export/about.{export.about_locale}.xhtml",
                content_hash(read_text(f"export/about.{export.about_locale}.xhtml")),
            ),
        )
    identity: dict[str, Any] = {
        "schema_version": 1,
        "source": context.source,
        "target": context.target,
        "phase": context.phase,
        "path": context.path,
        "operations": [
            {
                key: value
                for key, value in asdict(op).items()
                if key not in {"origins", "reason", "enabled"}
            }
            for op in selections
            if op.enabled
        ],
        "rules": effective_rules,
        "resources": resources,
    }
    if context.phase in {"analysis", "export"}:
        identity["source_identity"] = context.source_identity
    if context.phase == "export":
        identity.update(format=context.format, backend=context.backend, export=asdict(export))
        identity["output"] = {
            "bilingual": context.bilingual,
            "order": context.order,
            "preserve_source_style": context.preserve_source_style,
            "about_page": context.about_page,
        }
    semantic_identity = {key: value for key, value in identity.items() if key != "source_identity"}
    return PolicyPlan(
        context,
        tuple(selections),
        tuple(sorted(rules.items())),
        templates,
        resources,
        export,
        content_hash(identity),
        content_hash(semantic_identity),
    )
