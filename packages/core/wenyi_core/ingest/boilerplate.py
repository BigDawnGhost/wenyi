"""Optional ingest-time detection of scraped-site boilerplate paragraphs.

Matched paragraphs stay in state with their original segment indices and backfill anchors,
marked via ``segment.meta["skip"] = True`` so translation and export exclude them without
mutating document structure or preventing reversal.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..models.document import KIND_HEADING, Document, Segment

SKIP_META_KEY = "skip"
SKIP_REASON_META_KEY = "skip_reason"
_MAX_BUILTIN_LENGTH = 120
_MAX_PREVIEW_SAMPLES = 50

# Words that indicate normal English prose rather than an ad-network attribution line.
_PROSE_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "my",
        "our",
        "your",
        "his",
        "her",
        "its",
        "their",
        "this",
        "that",
        "these",
        "those",
        "all",
        "some",
        "any",
        "no",
        "every",
        "each",
    }
)

_AD_BY_RE = re.compile(
    r"^\s*ads?\s+by\s+([A-Za-z0-9][A-Za-z0-9._-]{1,30}(?:\s+[A-Za-z0-9._-]{1,20})?)\s*$",
    re.IGNORECASE,
)

_PROMO = (
    r"(?:好书|尽在|免费|下载|电子书|全文|小说|看书|阅读|更多|精彩|正版|首发|更新|书友|"
    r"\b(?:txt|epub|mobi|azw3|pdf)\b)"
)
_URL = r"(?:https?://|www\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?"
_DECOR = r"[\s\[\]【】()（）<>《》*_~!！。.,，-]"

_ZH_WATERMARK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Remember our book site / domain / URL" banners.
    re.compile(
        r"记住.{0,6}(?:我们|本站|域名|网址).{0,24}"
        r"(?:看书|小说|书吧|书网|文学|中文|阅读|网|站|com|net|org|cc|cn|la|tv|me)",
        re.IGNORECASE,
    ),
    # Obfuscated "please bookmark our site" prompts, e.g. "请...您....收藏_6191书1吧…".
    re.compile(
        r"(?:请|欢迎|敬请).{0,16}(?:收藏|访问|登录|记住|关注).{0,24}"
        r"(?:书.{0,4}(?:吧|网|阁|屋|轩|城)|小说|文学|网址|域名|本站)",
        re.IGNORECASE,
    ),
    # Common Chinese web-novel scraper footers/headers referencing updates/no-popups/domains.
    re.compile(
        r"(?:最新章节|无弹窗|全文阅读|笔趣阁|看书网|顶点小说|飘天文学|八一中文|顶点文学|手机阅读|移动版)"
        r".{0,24}(?:首发|更新|网址|域名|阅读|提供|www\.|http|\.com|\.net|\.cc|\.cn)",
        re.IGNORECASE,
    ),
    # Bare standalone URL lines (with optional decorative brackets or punctuation).
    re.compile(
        r"^[\s\[\]【】()（）<>《》*_~-]{0,6}"
        r"(?:https?://|www\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?"
        r"[\s\[\]【】()（）<>《》*_~-]{0,6}$",
        re.IGNORECASE,
    ),
    # Short promo/format watermarks embedding a domain, e.g. "好书尽在www.qinkan.net"
    # or "Txt,Epub,Mobi www.qinkan.net".
    # The whole line must be the watermark, so story text that mentions a URL survives.
    re.compile(
        rf"^{_DECOR}*(?:{_PROMO}.{{0,24}}{_URL}"
        rf"|{_URL}.{{0,24}}{_PROMO}(?:{_PROMO}|{_DECOR})*){_DECOR}*$",
        re.IGNORECASE,
    ),
)

_STRAY_FRAGMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Lone scraper angle-bracket / pipe markers such as ">" or ">>".
    re.compile(r"^\s*[><|]{1,3}\s*$"),
    # Garbled punctuation-plus-short-ASCII scraper artifacts such as "，.??m".
    re.compile(r"^\s*[,，。\.?!？！;；:：~`^\\/_]{2,}[A-Za-z]{1,3}\s*$"),
)


def compile_boilerplate_patterns(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    """Compile user-supplied boilerplate regular expressions with clear errors."""
    compiled: list[re.Pattern[str]] = []
    for index, raw in enumerate(patterns):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"pipeline.boilerplate_patterns[{index}] must be a non-empty regex")
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            raise ValueError(
                f"Invalid regex in pipeline.boilerplate_patterns[{index}] ({raw!r}): {exc}"
            ) from exc
    return tuple(compiled)


def match_boilerplate(
    text: str,
    *,
    custom_patterns: Sequence[re.Pattern[str]] = (),
    include_builtin: bool = True,
) -> str | None:
    """Return the matched boilerplate rule name for ``text``, or None for story text."""
    stripped = text.strip()
    if not stripped:
        return None

    for index, pattern in enumerate(custom_patterns):
        if pattern.search(stripped):
            return f"custom:{index}"

    if not include_builtin or len(stripped) > _MAX_BUILTIN_LENGTH:
        return None

    ad_match = _AD_BY_RE.match(stripped)
    if ad_match:
        words = ad_match.group(1).split()
        if words and words[0].lower() not in _PROSE_STOPWORDS:
            return "builtin:ad_network_marker"

    for pattern in _ZH_WATERMARK_PATTERNS:
        if pattern.search(stripped):
            return "builtin:zh_site_watermark"

    for pattern in _STRAY_FRAGMENT_PATTERNS:
        if pattern.match(stripped):
            return "builtin:scraper_stray_fragment"

    return None


def _logical_groups(segments: list[Segment]) -> list[list[Segment]]:
    groups: list[list[Segment]] = []
    for segment in segments:
        if segment.cont and groups:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    return groups


def mark_boilerplate_segments(
    document: Document,
    *,
    enabled: bool,
    custom_patterns: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Mark matching paragraphs with ``meta['skip'] = True`` without altering segment identities."""
    if not enabled:
        return []
    compiled = compile_boilerplate_patterns(custom_patterns)
    skipped: list[dict[str, Any]] = []
    for chapter in document.chapters:
        for group in _logical_groups(chapter.segments):
            joined = "".join(seg.source for seg in group)
            if not joined.strip():
                continue
            # Built-in rules never skip chapter headings; custom regexes apply to any segment.
            is_heading = group[0].kind == KIND_HEADING
            rule = match_boilerplate(
                joined,
                custom_patterns=compiled,
                include_builtin=not is_heading,
            )
            if rule is None:
                continue
            for seg in group:
                seg.meta[SKIP_META_KEY] = True
                seg.meta[SKIP_REASON_META_KEY] = rule
            skipped.append(
                {
                    "chapter_index": chapter.index,
                    "segment_index": group[0].index,
                    "rule": rule,
                    "source": joined.strip()[:120],
                }
            )
    if skipped:
        document.meta["skipped_boilerplate_count"] = len(skipped)
        document.meta["skipped_boilerplate"] = skipped[:_MAX_PREVIEW_SAMPLES]
    else:
        document.meta.pop("skipped_boilerplate_count", None)
        document.meta.pop("skipped_boilerplate", None)
    return skipped
