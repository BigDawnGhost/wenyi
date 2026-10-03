"""Allowlisted operation contracts. Concrete implementations live in their own domains."""

from types import MappingProxyType

from .models import OperationBinding, OperationSpec


def validate_implementations(
    implementations: dict[str, tuple[str, ...]], model_operations: tuple[str, ...]
) -> None:
    """Validate explicit domain adapters and referenced model IDs at bootstrap."""
    for spec in OPERATIONS.values():
        if spec.id not in implementations.get(spec.point, ()):
            raise ValueError(
                f"Missing language operation implementation: {spec.id} at {spec.point}"
            )
        if any(operation not in model_operations for operation in spec.llm_operations):
            raise ValueError(f"Unregistered model operation required by {spec.id}")


OPERATIONS = MappingProxyType(
    {
        spec.id: spec
        for spec in (
            OperationSpec("prompt.language_rules", "prompt.compose", writes=("language_rules",)),
            OperationSpec(
                "punctuation.zh_cn",
                "export.text",
                roles=("target", "pair"),
                languages=("zh",),
                paths=("book",),
                exclusive_group="target_punctuation",
            ),
            OperationSpec(
                "docx.chinese_font",
                "export.style",
                roles=("target", "pair"),
                languages=("zh", "zh-Hant"),
                paths=("book",),
                formats=("docx",),
                options_schema=(("font", "str", "宋体"),),
                writes=("target_font",),
            ),
            OperationSpec(
                "markup.japanese_ruby",
                "export.source_markup",
                roles=("source", "pair"),
                languages=("ja",),
                paths=("book",),
                formats=("epub", "html", "pdf"),
                backends=("native", "weasyprint", "fpdf2"),
                writes=("preserve_source_ruby",),
            ),
            OperationSpec(
                "export.language_metadata",
                "export.metadata",
                roles=("target", "pair"),
                paths=("book",),
                writes=("language_tag", "about_locale"),
            ),
        )
    }
)


def validate_bindings(bindings: dict[str, OperationBinding]) -> None:
    """Reject invalid IDs/options even when that operation would be skipped for this path."""
    for operation, binding in bindings.items():
        if operation not in OPERATIONS:
            raise ValueError(f"Unknown language operation: {operation}")
        if operation in {"prompt.language_rules", "export.language_metadata"}:
            if binding.mode == "off":
                raise ValueError(f"Required language operation cannot be disabled: {operation}")
        validate_options(OPERATIONS[operation], binding)


def validate_options(spec: OperationSpec, binding: OperationBinding) -> tuple[tuple[str, str], ...]:
    fields = {name: (kind, default) for name, kind, default in spec.options_schema}
    unknown = set(binding.options) - set(fields)
    if unknown:
        raise ValueError(f"Unknown option for {spec.id}: {', '.join(sorted(unknown))}")
    result = []
    for name, (kind, default) in fields.items():
        value = binding.options.get(name, default)
        if kind != "str" or not isinstance(value, str) or not value.strip():
            raise ValueError(f"{spec.id}.{name} must be a nonempty string")
        result.append((name, value))
    return tuple(sorted(result))
