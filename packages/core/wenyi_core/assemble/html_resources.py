"""Embed HTML media or package it for EPUB from one explicit local/container namespace.
Keep existing data URIs intact in single-file output and never fetch remote references.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import os
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from .export_view import AssembleStore

# File kinds that travel inside the source container and must be packaged beside the
# export. XHTML/OPF/NCX stay out so the whole book is never resident in memory.
_MEDIA_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".mp4",
        ".webm",
        ".ogv",
        ".mp3",
        ".ogg",
        ".wav",
        ".m4a",
        ".pdf",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
    }
)

# Image MIME types mapped to extensions.
_IMAGE_EXTENSION_BY_TYPE = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}

# HTML tags and resource attributes for bulk scanning and reference rewriting.
_RESOURCE_ATTRS = {
    "img": ("src",),
    "source": ("src",),
    "object": ("data",),
    "embed": ("src",),
    "audio": ("src",),
    "video": ("src", "poster"),
    "image": ("href", "xlink:href"),
}

_DATA_URI = re.compile(
    r"^data:(?P<media>[-\w.+/]+)?(?P<params>(?:;[-\w.+]+=[^;,]+)*)(?P<b64>;base64)?,(?P<data>.*)$",
    re.DOTALL | re.IGNORECASE,
)


def _resource_extension(media_type: str, reference: str) -> str:
    """Return a stable extension for a packaged HTML resource."""
    extension = os.path.splitext(urlsplit(reference).path)[1].lower()
    if extension and len(extension) <= 10:
        return extension
    return _IMAGE_EXTENSION_BY_TYPE.get(
        media_type,
        mimetypes.guess_extension(media_type) or ".bin",
    )


def _load_html_resource(
    reference: str,
    *,
    source_dir: str,
    zip_resources: dict[str, bytes] | None = None,
) -> tuple[str, bytes] | None:
    """Load a data URI or a resource from exactly one source namespace.

    ``zip_resources=None`` selects local files contained beneath ``source_dir``.
    Any mapping, including an empty one, selects ZIP-only lookup: ``source_dir`` is
    a container-internal POSIX directory, never a filesystem path. References resolve
    against that directory first and then as given, with no disk fallback.
    """
    value = reference.strip()
    if not value or value.startswith(("#", "http://", "https://", "//")):
        return None
    match = _DATA_URI.match(value)
    if match:
        media_type = (match.group("media") or "application/octet-stream").lower()
        raw = match.group("data")
        try:
            payload = (
                base64.b64decode(raw, validate=True)
                if match.group("b64")
                else unquote(raw).encode("utf-8")
            )
        except (ValueError, binascii.Error):
            return None
        return media_type, payload

    parsed = urlsplit(value)
    relative = unquote(parsed.path)
    if zip_resources is not None:
        if parsed.scheme:
            return None
        for key in (
            posixpath.normpath(posixpath.join(source_dir, relative)) if relative else source_dir,
            posixpath.normpath(relative),
        ):
            if key in zip_resources:
                media_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
                return media_type, zip_resources[key]
        return None

    if parsed.scheme and parsed.scheme != "file":
        return None
    candidate = os.path.abspath(os.path.join(source_dir, relative))
    try:
        contained = os.path.commonpath((candidate, os.path.abspath(source_dir))) == os.path.abspath(
            source_dir
        )
    except ValueError:
        contained = False
    if contained and os.path.isfile(candidate):
        media_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        return media_type, Path(candidate).read_bytes()
    return None


def _srcset_candidates(value: str) -> list[tuple[str, str]]:
    """Read URL/descriptor pairs without splitting the commas inside data URLs."""
    space = "\t\n\f\r "
    candidates = []
    position = 0
    while position < len(value):
        while position < len(value) and value[position] in space + ",":
            position += 1
        start = position
        while position < len(value) and value[position] not in space:
            position += 1
        reference = value[start:position]
        if not reference:
            break
        if reference.endswith(","):
            candidates.append((reference.rstrip(","), ""))
            continue
        start = position
        while position < len(value) and value[position] != ",":
            position += 1
        candidates.append((reference, value[start:position].strip(space)))
    return candidates


def _package_html_resources(
    html: str,
    *,
    source_dir: str,
    href_prefix: str,
    zip_resources: dict[str, bytes] | None = None,
    inline: bool = False,
) -> tuple[str, dict[str, tuple[str, bytes]]]:
    """Package references using the exclusive source contract of ``_load_html_resource``."""
    soup = BeautifulSoup(html, "html.parser")
    packaged: dict[str, tuple[str, bytes]] = {}
    href_by_reference: dict[str, str] = {}

    def package(reference: str) -> str:
        if inline and reference.lstrip().lower().startswith("data:"):
            return reference
        if reference in href_by_reference:
            return href_by_reference[reference]
        loaded = _load_html_resource(reference, source_dir=source_dir, zip_resources=zip_resources)
        if loaded is None:
            return reference
        media_type, payload = loaded
        if inline:
            href = f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}"
            href_by_reference[reference] = href
            return href
        digest = hashlib.sha256(payload).hexdigest()[:16]
        extension = _resource_extension(media_type, reference)
        href = posixpath.join(href_prefix, f"{digest}{extension}")
        packaged[href] = (media_type, payload)
        href_by_reference[reference] = href
        return href

    for tag_name, attrs in _RESOURCE_ATTRS.items():
        for element in soup.find_all(tag_name):
            for attr in attrs:
                value = element.get(attr)
                if isinstance(value, str):
                    element[attr] = package(value)
            srcset = element.get("srcset")
            if isinstance(srcset, str):
                element["srcset"] = ", ".join(
                    f"{package(reference)} {descriptor}".rstrip()
                    for reference, descriptor in _srcset_candidates(srcset)
                )
    return str(soup), packaged


def _embed_html_resources(
    html: str,
    *,
    source_dir: str,
    zip_resources: dict[str, bytes] | None = None,
) -> str:
    """Inline readable local/container media while retaining data and remote references."""
    rewritten, _packaged = _package_html_resources(
        html,
        source_dir=source_dir,
        href_prefix="",
        zip_resources=zip_resources,
        inline=True,
    )
    return rewritten


def _template_resource_source(
    store: AssembleStore,
    manifest: dict,
    source_path: str,
) -> str:
    """Return the HTML file whose directory resolves template media references."""
    if manifest.get("fmt") == "pdf":
        from ..ingest.pdf_reader import pdf_cache_html_path

        source_hash = manifest.get("source_sha256")
        if not isinstance(source_hash, str):
            raise ValueError("PDF manifest has no valid source_sha256")
        return pdf_cache_html_path(store.source_dir, source_hash)
    return source_path
