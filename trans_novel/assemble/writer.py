"""回填：把译文写回原格式。

本模块是 assemble 的公共入口；实现按格式拆在子模块：
- writer_common：路径、标题、语言等通用辅助
- text_writer：TXT / Markdown
- html_renderer：DOM 渲染
- html_resources：资源读取与物化
- html_writer：HTML 输出
- pdf_writer：PDF 输出
- docx_writer：Word .docx 重建
- epub_writer：EPUB 回填与新建

``assemble()`` / ``bilingual_out_path()`` 与少量私有辅助仍从此处导出；
替换实现细节的测试应直接 patch 对应子模块。
"""

from __future__ import annotations

from ..pipeline.runstore import RunStore
from .about import append_about_page
from .docx_writer import _assemble_docx
from .epub_writer import (
    _assemble_epub,
    _build_epub_from_chapters,
    _build_epub_from_html_templates,
    _inject_bilingual_style,
    _rewrite_html_document,
    _rewrite_toc,
)
from .html_renderer import _render_chapter_html, _render_segments_html
from .html_writer import _assemble_html
from .pdf_writer import _assemble_pdf, _normalize_html_for_fpdf
from .text_writer import _assemble_markdown, _assemble_text
from .writer_common import (
    _OUT_EXT,
    _default_out,
    _ensure_parent_dir,
    _epub_lang,
    bilingual_out_path,
)

__all__ = [
    "assemble",
    "bilingual_out_path",
    "_default_out",
    "_inject_bilingual_style",
    "_normalize_html_for_fpdf",
    "_render_chapter_html",
    "_render_segments_html",
    "_rewrite_html_document",
    "_rewrite_toc",
]


def assemble(
    store: RunStore,
    source_path: str,
    out_path: str | None = None,
    out_format: str = "epub",
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    about_page: bool = True,
    pdf_engine: str = "weasyprint",
) -> str:
    """生成译文文件（默认 EPUB）。

    out_format="epub"（默认）：
      - 原文是 EPUB → 按原模板回填，保留排版/资源；
      - 原文是纯文本 → 生成一个规范的 EPUB（标题 h1 + 段落 p）。
    out_format="txt"：无论原文格式，按章重建为纯文本。
    out_format="html"：优先回填 HTML 模板，无模板时按章重建。
    out_format="markdown"：无论原文格式，按章重建为 Markdown。
    out_format="pdf"：先生成打印专用 HTML，再由 WeasyPrint 分页输出。
    out_format="docx"：按章重建 Word 文档（标题导航 + 段落 + 简易表格）。
    bilingual=True 时额外输出原文，order 控制译文/原文先后。
    preserve_source_style=True 时原文继承原书正文样式，不注入淡化 CSS。
    about_page=True 时在书末附加"关于此翻译"说明页。
    """
    if out_format not in _OUT_EXT:
        supported = " / ".join(_OUT_EXT)
        raise ValueError(f"不支持的输出格式：{out_format}（支持 {supported}）")

    m = store.load_manifest()
    if out_format == "txt":
        out_path = out_path or _default_out(source_path, "txt", "", bilingual=bilingual)
        _ensure_parent_dir(out_path)
        return _assemble_text(store, out_path, bilingual=bilingual, order=order)
    if out_format == "html":
        out_path = out_path or _default_out(source_path, "html", "", bilingual=bilingual)
        _ensure_parent_dir(out_path)
        return _assemble_html(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if out_format == "markdown":
        out_path = out_path or _default_out(source_path, "markdown", "", bilingual=bilingual)
        _ensure_parent_dir(out_path)
        return _assemble_markdown(store, out_path, bilingual=bilingual, order=order)
    if out_format == "pdf":
        out_path = out_path or _default_out(source_path, "pdf", "", bilingual=bilingual)
        _ensure_parent_dir(out_path)
        return _assemble_pdf(
            store,
            source_path,
            out_path,
            engine=pdf_engine,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if out_format == "docx":
        out_path = out_path or _default_out(source_path, "docx", "", bilingual=bilingual)
        _ensure_parent_dir(out_path)
        return _assemble_docx(store, out_path, bilingual=bilingual, order=order)
    out_path = out_path or _default_out(source_path, "epub", "", bilingual=bilingual)
    _ensure_parent_dir(out_path)
    if m["fmt"] == "epub":
        result = _assemble_epub(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    elif m["fmt"] in {"html", "pdf"}:
        result = _build_epub_from_html_templates(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    else:
        # FB2 / text → 从章节数据生成规范 EPUB
        result = _build_epub_from_chapters(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if about_page:
        append_about_page(result, _epub_lang(m.get("target_lang", "zh")))
    return result
