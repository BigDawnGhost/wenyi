from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Literal

from trans_novel.assemble.epub.rendering.theme.contracts import ThemeBundle, ThemeError

_POLICY_VERSION = "epub-theme-v2"
_NOTE_PRESENTATION_POLICY_VERSION = "epub-note-marker-v1"
_OUTPUT_POLICY_VERSION = 2
_MAX_ASSET_BYTES = 256 * 1024
_BUILTINS = {
    "override_theme.styles": ("builtin:chinese-reading", "chinese-reading.css"),
    "bilingual_styles": ("builtin:bilingual", "bilingual.css"),
}


def _fail(detail: str, slot: str) -> ThemeError:
    return ThemeError("theme_config", detail, resource=slot)


def _validate_bytes(data: bytes, slot: str) -> bytes:
    if len(data) > _MAX_ASSET_BYTES:
        raise _fail("asset_too_large", slot)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _fail("asset_not_utf8", slot) from error
    return data


def _read_builtin(filename: str, slot: str) -> bytes:
    asset = resources.files("trans_novel.assemble.epub.rendering.theme").joinpath(
        "assets", filename
    )
    try:
        with asset.open("rb") as stream:
            return _validate_bytes(stream.read(_MAX_ASSET_BYTES + 1), slot)
    except FileNotFoundError as error:
        raise _fail("asset_not_found", slot) from error
    except OSError as error:
        raise _fail("asset_unreadable", slot) from error


def _resolve_path(value: str, base_dir: str | Path | None, slot: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _fail("invalid_asset_path", slot)
    if value.startswith("builtin:"):
        raise _fail("unknown_builtin", slot)
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            if base_dir is None:
                raise _fail("missing_base_dir", slot)
            path = Path(base_dir).expanduser() / path
        return path.resolve(strict=False)
    except ThemeError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise _fail("invalid_asset_path", slot) from error


def _read_custom(value: str, base_dir: str | Path | None, slot: str) -> tuple[bytes, str]:
    path = _resolve_path(value, base_dir, slot)
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_ASSET_BYTES + 1)
    except FileNotFoundError as error:
        raise _fail("asset_not_found", slot) from error
    except OSError as error:
        raise _fail("asset_unreadable", slot) from error
    return _validate_bytes(data, slot), str(path)


def _load_selection(
    value: str,
    slot: str,
    base_dir: str | Path | None,
) -> tuple[bytes, str, bool]:
    builtin, filename = _BUILTINS[slot]
    if value == builtin:
        return _read_builtin(filename, slot), builtin, False
    data, label = _read_custom(value, base_dir, slot)
    return data, label, True


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_theme(
    styles: str | None,
    bilingual_styles: str | None,
    *,
    config_base_dir: str | Path | None = None,
    origins: Mapping[str, str] | None = None,
) -> ThemeBundle:
    """固定本次使用的 CSS，并记录配置来源。"""
    selected: list[tuple[str, bytes, str, bool]] = []
    for slot, value in (
        ("override_theme.styles", styles),
        ("bilingual_styles", bilingual_styles),
    ):
        if value is not None:
            data, label, custom = _load_selection(value, slot, config_base_dir)
            selected.append((slot, data, label, custom))

    by_slot = {slot: data for slot, data, _, _ in selected}
    provenance: list[tuple[str, str]] = []
    for slot, _, label, custom in selected:
        provenance.append((slot, label))
        if custom and origins is not None and slot in origins:
            provenance.append((f"{slot}.origin", origins[slot]))

    general_css = by_slot.get("override_theme.styles")
    bilingual_css = by_slot.get("bilingual_styles")
    note_markers = styles == "builtin:chinese-reading"
    digest = _canonical_digest(
        {
            "general_css": (
                hashlib.sha256(general_css).hexdigest() if general_css is not None else None
            ),
            "bilingual_css": (
                hashlib.sha256(bilingual_css).hexdigest() if bilingual_css is not None else None
            ),
            "policy_version": _POLICY_VERSION,
            **(
                {"note_presentation_policy": _NOTE_PRESENTATION_POLICY_VERSION}
                if note_markers
                else {}
            ),
        }
    )
    return ThemeBundle(
        general_css=general_css,
        bilingual_css=bilingual_css,
        digest=digest,
        policy_version=_POLICY_VERSION,
        provenance=tuple(provenance),
        note_markers=note_markers,
    )


def semantic_output_digest(
    bundle: ThemeBundle | None,
    *,
    out_format: Literal["epub", "txt"],
    mono: bool,
    bilingual: bool,
    bilingual_order: Literal["target_first", "source_first"],
    layout_digest: str | None = None,
) -> str:
    return _canonical_digest(
        {
            "format": out_format,
            "mono": mono,
            "bilingual": bilingual,
            "bilingual_order": bilingual_order,
            "theme": (
                bundle.digest
                if out_format == "epub"
                and bundle is not None
                and (bundle.general_css is not None or bundle.bilingual_css is not None)
                else None
            ),
            "layout": (
                layout_digest
                if out_format == "epub" and bundle is not None and bundle.general_css is not None
                else None
            ),
            "output_policy": _OUTPUT_POLICY_VERSION,
        }
    )
