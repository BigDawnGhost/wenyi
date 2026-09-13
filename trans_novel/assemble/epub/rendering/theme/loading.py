from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from importlib import metadata, resources
from pathlib import Path
from typing import Literal

from trans_novel.assemble.epub.rendering.theme.contracts import ThemeBundle, ThemeError

_API_VERSION = 1
_POLICY_VERSION = "epub-theme-v1"
_OUTPUT_POLICY_VERSION = 1
_MAX_ASSET_BYTES = 256 * 1024
_BUILTINS = {
    "override_theme.rules": ("builtin:general", "general.js"),
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
    rules: str | None,
    styles: str | None,
    bilingual_styles: str | None,
    *,
    config_base_dir: str | Path | None = None,
    origins: Mapping[str, str] | None = None,
) -> ThemeBundle:
    """固定本次使用的资源内容；使用配置字段及其 ``.origin`` 标签记录来源。"""
    if (rules is None) != (styles is None):
        raise ThemeError("theme_config", "theme_pair_required")

    selected: list[tuple[str, bytes, str, bool]] = []
    for slot, value in (
        ("override_theme.rules", rules),
        ("override_theme.styles", styles),
        ("bilingual_styles", bilingual_styles),
    ):
        if value is not None:
            data, label, custom = _load_selection(value, slot, config_base_dir)
            selected.append((slot, data, label, custom))

    engine_version = ""
    if selected:
        try:
            engine_version = metadata.version("quickjs-ng")
        except metadata.PackageNotFoundError as error:
            raise ThemeError("theme_config", "engine_not_installed") from error

    by_slot = {slot: data for slot, data, _, _ in selected}
    provenance: list[tuple[str, str]] = []
    for slot, _, label, custom in selected:
        provenance.append((slot, label))
        if custom and origins is not None and slot in origins:
            provenance.append((f"{slot}.origin", origins[slot]))

    script = by_slot.get("override_theme.rules")
    general_css = by_slot.get("override_theme.styles")
    bilingual_css = by_slot.get("bilingual_styles")
    digest = _canonical_digest(
        {
            "script": hashlib.sha256(script).hexdigest() if script is not None else None,
            "general_css": (
                hashlib.sha256(general_css).hexdigest() if general_css is not None else None
            ),
            "bilingual_css": (
                hashlib.sha256(bilingual_css).hexdigest() if bilingual_css is not None else None
            ),
            "api_version": _API_VERSION,
            "policy_version": _POLICY_VERSION,
            "engine_version": engine_version,
        }
    )
    return ThemeBundle(
        script=script,
        general_css=general_css,
        bilingual_css=bilingual_css,
        digest=digest,
        engine_version=engine_version,
        api_version=_API_VERSION,
        policy_version=_POLICY_VERSION,
        provenance=tuple(provenance),
    )


def semantic_output_digest(
    bundle: ThemeBundle | None,
    *,
    out_format: Literal["epub", "txt"],
    mono: bool,
    bilingual: bool,
    bilingual_order: Literal["target_first", "source_first"],
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
                and (
                    bundle.script is not None
                    or bundle.general_css is not None
                    or bundle.bilingual_css is not None
                )
                else None
            ),
            "output_policy": _OUTPUT_POLICY_VERSION,
        }
    )
