"""解析单次调用中持久化的输出选择。"""

from __future__ import annotations

from collections.abc import Mapping

from trans_novel.config import OutputConfig, resolve_output
from trans_novel.pipeline.state import (
    SavedOutputSelection,
    load_output_selection,
    save_output_selection,
    source_bytes_hash,
)

_THEME_SLOTS = (
    "override_theme.styles",
    "bilingual_styles",
)


def load_effective_output(
    store,
    current: OutputConfig,
    current_origins: Mapping[str, str],
    *,
    input_path: str,
    identity_languages: tuple[str, str],
    out_format: str,
) -> tuple[OutputConfig, dict[str, str], bool, str]:
    """核验运行身份并读取与源文件绑定的有效输出选择。"""
    source_sha = source_bytes_hash(input_path)
    if store.exists():
        store.verify_identity(
            source_bytes_sha256=source_sha,
            source_lang=identity_languages[0],
            target_lang=identity_languages[1],
        )
    saved = load_output_selection(store, source_bytes_sha256=source_sha)
    output, origins, normalized = resolve_effective_output(
        current, current_origins, saved, out_format=out_format
    )
    return output, origins, normalized, source_sha


def save_effective_output(
    store,
    output: OutputConfig,
    origins: Mapping[str, str],
    *,
    source_sha: str,
    normalized: bool,
) -> None:
    """保存已通过预检的规范输出选择。"""
    save_output_selection(
        store,
        output.model_dump(mode="json"),
        origins,
        source_bytes_sha256=source_sha,
    )
    if normalized:
        store.log_event("output_selection_normalized", mono=True, bilingual=False)


def resolve_effective_output(
    current: OutputConfig,
    current_origins: Mapping[str, str],
    saved: SavedOutputSelection | None,
    *,
    out_format: str,
) -> tuple[OutputConfig, dict[str, str], bool]:
    """按当前显式值、已保存值、默认值解析本次输出快照。"""
    saved_output = (
        OutputConfig.model_validate(saved.selection, strict=True) if saved is not None else None
    )
    effective, normalized = resolve_output(current, saved_output)

    preserve_saved_theme = out_format == "txt" and saved_output is not None
    if preserve_saved_theme:
        effective.override_theme = (
            saved_output.override_theme.model_copy(deep=True)
            if saved_output.override_theme is not None
            else None
        )
        effective.bilingual_styles = saved_output.bilingual_styles

    origins: dict[str, str] = {}
    for slot in _THEME_SLOTS:
        root = slot.partition(".")[0]
        if preserve_saved_theme:
            origin = saved.origins.get(slot) if saved is not None else None
        elif root in current.model_fields_set:
            origin = current_origins.get(slot)
        else:
            origin = saved.origins.get(slot) if saved is not None else None
        if origin is not None and _custom_value(effective, slot):
            origins[slot] = origin
    return effective, origins, normalized


def _custom_value(output: OutputConfig, slot: str) -> bool:
    if slot == "bilingual_styles":
        value = output.bilingual_styles
    elif output.override_theme is None:
        return False
    else:
        value = getattr(output.override_theme, slot.rsplit(".", 1)[1])
    return not value.startswith("builtin:")


__all__ = ["load_effective_output", "resolve_effective_output", "save_effective_output"]
