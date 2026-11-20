"""FictionBook encoding policy and native chapter/resource extraction."""

from __future__ import annotations

import base64
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..markup import _epub_native
from .encoding import decode_source
from .models import Document

_DECLARED_ENCODING = re.compile(rb"<\?xml.*?encoding\s*=\s*['\"]([^'\"]+)['\"]")


def _read_source(path: str) -> str:
    """Keep the strict declared-encoding and legacy ambiguity policy."""
    raw = Path(path).read_bytes()
    if match := _DECLARED_ENCODING.search(raw):
        encoding = match[1].decode("ascii", errors="replace")
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    text, _encoding = decode_source(raw)
    return text


def peek_fb2_title(path: str) -> str:
    """Read the title without creating chapter state."""
    content = _read_source(path)
    try:
        return _epub_native.fb2_title(content, os.path.splitext(os.path.basename(path))[0])
    except ValueError as exc:
        raise ET.ParseError(str(exc)) from exc


def read_fb2(path: str, source_lang: str, target_lang: str) -> Document:
    """Use the native FB2 parser without changing chapter or segment identities."""
    content = _read_source(path)
    try:
        data = _epub_native.parse_fb2(
            content,
            os.path.splitext(os.path.basename(path))[0],
            os.path.abspath(path),
            source_lang,
            target_lang,
        )
    except ValueError as exc:
        raise ET.ParseError(str(exc)) from exc
    return Document.model_validate_json(data)


def read_fb2_binaries(path: str) -> dict[str, tuple[str, bytes]]:
    """Extract embedded data at export time; never persist it in chapter state."""
    content = _read_source(path)
    try:
        binaries = _epub_native.fb2_binaries(content)
    except ValueError as exc:
        raise ET.ParseError(str(exc)) from exc
    resources = {}
    for resource_id, content_type, value in binaries:
        try:
            data = base64.b64decode("".join(value.split()), validate=False)
        except (ValueError, TypeError):
            continue
        resources[resource_id] = (content_type, data)
    return resources
