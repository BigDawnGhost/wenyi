"""HTML/FB2 资源读取与物化：data URI、本地文件引用、模板资源打包。

负责解析 HTML 模板中的图片/媒体引用，支持本地文件、data URI 和 FB2 binary，
将资源去重打包或物化到输出目录旁的 .assets 子目录。
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

from ..pipeline.runstore import RunStore

# 图片 MIME 类型到扩展名的映射
_IMAGE_EXTENSION_BY_TYPE = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}

# HTML 标签与资源属性的映射，用于批量扫描和重写引用
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
) -> tuple[str, bytes] | None:
    """Load a local or data-URI resource without escaping the HTML source tree."""
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
    if parsed.scheme and parsed.scheme != "file":
        return None
    relative = unquote(parsed.path)
    candidate = os.path.abspath(os.path.join(source_dir, relative))
    try:
        if os.path.commonpath((candidate, os.path.abspath(source_dir))) != os.path.abspath(
            source_dir
        ):
            return None
    except ValueError:
        return None
    if not os.path.isfile(candidate):
        return None
    media_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
    return media_type, Path(candidate).read_bytes()


def _package_html_resources(
    html: str,
    *,
    source_dir: str,
    href_prefix: str,
) -> tuple[str, dict[str, tuple[str, bytes]]]:
    """Rewrite image/media references and return packaged href -> payload entries."""
    soup = BeautifulSoup(html, "html.parser")
    packaged: dict[str, tuple[str, bytes]] = {}
    href_by_reference: dict[str, str] = {}

    def package(reference: str) -> str:
        if reference in href_by_reference:
            return href_by_reference[reference]
        loaded = _load_html_resource(reference, source_dir=source_dir)
        if loaded is None:
            return reference
        media_type, payload = loaded
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
            if isinstance(srcset, str) and not srcset.lstrip().startswith("data:"):
                rewritten = []
                for candidate in srcset.split(","):
                    bits = candidate.strip().split()
                    if bits:
                        bits[0] = package(bits[0])
                    rewritten.append(" ".join(bits))
                element["srcset"] = ", ".join(rewritten)
    return str(soup), packaged


def _materialize_html_resources(
    html: str,
    *,
    source_path: str,
    out_path: str,
) -> str:
    """Copy HTML media beside an exported document and rewrite its references."""
    asset_dir_name = f"{Path(out_path).stem}.assets"
    rewritten, packaged = _package_html_resources(
        html,
        source_dir=os.path.dirname(os.path.abspath(source_path)),
        href_prefix=asset_dir_name,
    )
    for href, (_media_type, payload) in packaged.items():
        destination = os.path.join(os.path.dirname(os.path.abspath(out_path)), *href.split("/"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as file:
            file.write(payload)
    return rewritten


def _template_resource_source(
    store: RunStore,
    manifest: dict,
    source_path: str,
) -> str:
    """Return the HTML file whose directory resolves template media references."""
    if manifest.get("fmt") == "pdf":
        from ..ingest.pdf_reader import pdf_cache_html_path

        source_hash = manifest.get("source_sha256")
        if not isinstance(source_hash, str):
            raise ValueError("PDF manifest 缺少有效的 source_sha256")
        return pdf_cache_html_path(store.source_dir, source_hash)
    return source_path
