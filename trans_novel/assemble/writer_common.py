"""通用辅助：输出路径、标题、语言、段落合并等跨格式共享逻辑。"""

from __future__ import annotations

import os
import re

from ..ingest.models import Chapter

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')

_OUT_EXT = {
    "epub": ".epub",
    "txt": ".txt",
    "html": ".html",
    "markdown": ".md",
    "pdf": ".pdf",
    "docx": ".docx",
}


def _sanitize_filename(name: str, fallback: str = "translated") -> str:
    """移除跨平台非法文件名字符，并限制名称长度。"""
    name = _ILLEGAL_FN.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def _ensure_parent_dir(path: str) -> None:
    """Create the output directory while allowing a bare filename."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def _default_out(
    source_path: str,
    out_format: str,
    title: str | None = None,
    *,
    bilingual: bool = False,
) -> str:
    """Return the default export path under the input file's ``output`` folder."""
    ext = _OUT_EXT.get(out_format, ".epub")
    output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), "output")
    os.makedirs(output_dir, exist_ok=True)
    if title and title.strip():
        # 保留给显式调用方使用；默认 assemble 不传书名译名。
        return os.path.join(output_dir, _sanitize_filename(title) + ext)
    base, _ = os.path.splitext(source_path)
    suffix = ".zh-bi" if bilingual else ".zh"
    return os.path.join(
        output_dir,
        f"{os.path.basename(base)}{suffix}{ext}",
    )


def bilingual_out_path(out_path: str) -> str:
    """调用方显式指定了 out_path 时，派生双语版路径：stem 追加 -bi。"""
    base, ext = os.path.splitext(out_path)
    return f"{base}-bi{ext}"


def _ch_title(c: dict) -> str:
    """章节展示标题：优先译名，回退原标题。"""
    return (c.get("title_translated") or c.get("title") or "").strip()


def _export_book_title(
    title: str | None,
    target_lang: str | None,
    *,
    bilingual: bool,
) -> str:
    """导出用书名：原书名后追加 Wenyi、目标语言及可选双语标记。"""
    base = (title or "").strip() or "translated"
    lang = (target_lang or "").strip().replace("_", "-").lower() or "zh"
    suffix = f"-wenyi-{lang}{'-bi' if bilingual else ''}"
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def _seg_text(seg) -> str:
    """返回有效译文；译文为空时回退到源文以避免丢内容。"""
    return seg.target if (seg.target and seg.target.strip()) else seg.source


def _epub_lang(lang: str | None) -> str:
    """EPUB 元数据语言码；中文目标默认标成简体中文。"""
    normalized = (lang or "").strip().replace("_", "-").lower()
    if normalized in {"", "zh", "zh-cn", "zh-hans", "cn"}:
        return "zh-Hans"
    return lang or "zh-Hans"


def _merged_paragraphs(chapter: Chapter) -> list[tuple[str, str, str]]:
    """把章内 Segment 合并为段落，cont 续段并回上一段。返回 [(kind, target, source), ...]。"""
    paras: list[list[str]] = []  # 每段累积的译文片段
    srcs: list[list[str]] = []  # 每段累积的原文片段
    kinds: list[str] = []
    for s in chapter.segments:
        if not s.source.strip():
            continue
        if s.cont and paras:
            paras[-1].append(_seg_text(s))
            srcs[-1].append(s.source)
        else:
            paras.append([_seg_text(s)])
            srcs.append([s.source])
            kinds.append(s.kind)
    return [(k, "".join(p), "".join(sr)) for k, p, sr in zip(kinds, paras, srcs)]


def _bilingual_source(source: str, target: str) -> str:
    """双语原文去重：原文为空白，或与译文相同（翻译回退到原文）时不输出原文。

    Segment.source 可能含振假名标记 ``〘…〙``；纯文本回退时剥掉，真正的
    ruby 仍由 ``_bilingual_source_markup`` 从模板 DOM 保留。
    """
    from ..ingest.epub_reader import strip_ruby_markers

    source = strip_ruby_markers(source)
    return source if (source.strip() and source != target) else ""


def _ordered_pair(source: str, target: str, order: str) -> tuple[str, str]:
    """按 order 返回双语排列顺序：source_first 时原文在前，否则译文在前。"""
    return (source, target) if order == "source_first" else (target, source)


def _manifest_target_lang(manifest: dict) -> str:
    """从 manifest 提取目标语言代码，默认 zh。"""
    raw = manifest.get("target_lang", "zh")
    return raw if isinstance(raw, str) else "zh"
