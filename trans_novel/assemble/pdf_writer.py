"""PDF 输出：WeasyPrint 和 fpdf2 两种引擎适配。

先调用 html_writer 生成打印用临时 HTML，再根据 pdf_engine 分派到
WeasyPrint（系统渲染库）或 fpdf2（纯 Python）路径。
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Comment, Tag

from ..pipeline.runstore import RunStore
from .html_writer import _assemble_html

# 打印专用 CSS：A5 版面、页码、CJK 字体和分页控制
_PRINT_CSS = """\
@page {
  size: A5;
  margin: 18mm 16mm 20mm;
  @bottom-center {
    content: counter(page);
    color: #666;
    font-size: 9pt;
  }
}
html, body {
  background: white !important;
  color: black;
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", serif;
  font-size: 10.5pt;
  line-height: 1.75;
}
body { margin: 0; padding: 0; max-width: none; }
h1 { break-before: page; }
h1:first-child { break-before: auto; }
h1, h2, h3, h4, h5, h6 { break-after: avoid; page-break-after: avoid; }
p { orphans: 2; widows: 2; }
img, svg, picture { max-width: 100%; height: auto; }
figure { break-inside: avoid; page-break-inside: avoid; }
"""


def _assemble_pdf_weasyprint(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Render a print-specific HTML export to PDF with WeasyPrint."""
    if sys.platform == "darwin":
        # uv's standalone Python does not always search Homebrew's library
        # directory, even when ``brew install weasyprint`` installed Pango.
        brew_libs = [
            path for path in ("/opt/homebrew/lib", "/usr/local/lib") if os.path.isdir(path)
        ]
        if brew_libs:
            existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(
                [*brew_libs, *([existing] if existing else [])]
            )
    try:
        HTML = getattr(importlib.import_module("weasyprint"), "HTML")
    except (ImportError, OSError, AttributeError) as error:
        raise ImportError(
            "实验性 PDF 输出需要 WeasyPrint，请运行：uv sync --extra pdf-output"
        ) from error

    with tempfile.TemporaryDirectory(prefix="trans-novel-pdf-") as directory:
        html_path = os.path.join(directory, "book.html")
        _assemble_html(
            store,
            source_path,
            html_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
        with open(html_path, encoding="utf-8") as file:
            soup = BeautifulSoup(file.read(), "html.parser")
        head = soup.find("head")
        if head is None:
            head = soup.new_tag("head")
            soup.insert(0, head)
        style = soup.new_tag("style", id="trans-novel-print-style")
        style.string = _PRINT_CSS
        head.append(style)
        HTML(string=str(soup), base_url=directory).write_pdf(out_path)
    return out_path


def _find_fpdf_font() -> str:
    """Find a user-specified or common cross-platform CJK font file."""
    configured = os.environ.get("TRANS_NOVEL_PDF_FONT", "").strip()
    candidates = [
        configured,
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    windows_dir = os.environ.get("WINDIR", "")
    if windows_dir:
        candidates.extend(
            [
                os.path.join(windows_dir, "Fonts", "msyh.ttc"),
                os.path.join(windows_dir, "Fonts", "simsun.ttc"),
            ]
        )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise RuntimeError(
        "fpdf2 PDF 输出需要中文字体。请设置 TRANS_NOVEL_PDF_FONT，"
        "指向一个包含中文字符的 TTF/OTF/TTC 字体文件。"
    )


def _normalize_html_for_fpdf(html: str, *, base_dir: str) -> str:
    """Reduce rendered book HTML to the subset supported by fpdf2."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body") or soup
    for comment in list(body.find_all(string=lambda node: isinstance(node, Comment))):
        comment.extract()
    for picture in list(body.find_all("picture")):
        image = picture.find("img")
        if isinstance(image, Tag):
            picture.replace_with(image.extract())
        else:
            picture.decompose()
    for figure in list(body.find_all("figure")):
        caption = figure.find("figcaption")
        if isinstance(caption, Tag):
            caption.name = "p"
            caption["align"] = "center"
        figure.unwrap()
    # fpdf2 renders ``img`` as a flowable rather than true inline content.
    # Split mixed paragraphs explicitly so media keeps its source order.
    for paragraph in list(body.find_all("p")):
        if not paragraph.find("img", recursive=False) or not paragraph.get_text(strip=True):
            continue
        replacements: list[Tag] = []
        current = soup.new_tag("p")
        current.attrs.update(paragraph.attrs)
        for child in list(paragraph.children):
            if isinstance(child, Tag) and child.name == "img":
                if current.get_text(strip=True):
                    replacements.append(current)
                replacements.append(child.extract())
                current = soup.new_tag("p")
                current.attrs.update(paragraph.attrs)
            else:
                current.append(child.extract())
        if current.get_text(strip=True):
            replacements.append(current)
        for replacement in replacements:
            paragraph.insert_before(replacement)
        paragraph.decompose()
    for element in list(body.find_all(["source", "style", "script"])):
        element.decompose()
    for image in body.find_all("img"):
        src = image.get("src")
        if isinstance(src, str) and not urlsplit(src).scheme:
            absolute_src = os.path.abspath(os.path.join(base_dir, src))
            image["src"] = absolute_src
            natural_width = 0
            if absolute_src.lower().endswith(".svg"):
                try:
                    svg_head = Path(absolute_src).read_text(encoding="utf-8", errors="ignore")[
                        :2048
                    ]
                    width_match = re.search(
                        r"<svg[^>]*\bwidth=['\"]?([0-9.]+)",
                        svg_head,
                        re.IGNORECASE,
                    )
                    natural_width = round(float(width_match.group(1))) if width_match else 0
                except (OSError, ValueError):
                    natural_width = 0
            else:
                try:
                    open_image = getattr(importlib.import_module("PIL.Image"), "open")

                    with open_image(absolute_src) as raster:
                        natural_width = raster.width
                except (ImportError, OSError, AttributeError):
                    natural_width = 0
            image["width"] = str(min(natural_width or 340, 340))
    # fpdf2's HTML parser assumes every <a> tag has href and raises KeyError
    # for source books that use anchor tags solely as styling wrappers. Keep any
    # id/name metadata on a neutral span instead of deleting it during cleanup;
    # fpdf2 does not guarantee that such metadata becomes a live PDF destination.
    for anchor in list(body.find_all("a")):
        href = anchor.get("href")
        if not isinstance(href, str) or not href.strip():
            anchor.attrs.pop("href", None)
            if anchor.get("id") or anchor.get("name"):
                anchor.name = "span"
            else:
                anchor.unwrap()
    # fpdf2's table renderer can fail on narrow cells in scanned/manual-style
    # books ("Not enough horizontal space to render a single character").
    # Remove only the table structure, from the innermost table outward. Moving
    # the existing nodes instead of calling get_text() preserves links, images,
    # emphasis and explicit line breaks.
    for table in reversed(list(body.find_all("table"))):
        if table.parent is None:
            continue
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            for index, cell in enumerate(cells):
                if index:
                    cell.insert_before(" | ")
                if cell.name == "th":
                    strong = soup.new_tag("strong")
                    for child in list(cell.contents):
                        strong.append(child.extract())
                    cell.append(strong)
                cell.unwrap()
            row.append(soup.new_tag("br"))
            row.unwrap()
        caption = table.find("caption", recursive=False)
        if isinstance(caption, Tag):
            caption.append(soup.new_tag("br"))
            caption.unwrap()
        for column in list(table.find_all(["col", "colgroup"])):
            column.decompose()
        for structural in list(table.find_all(["thead", "tbody", "tfoot", "tr", "th", "td"])):
            if structural.parent is not None:
                structural.unwrap()
        table.unwrap()
    for element in list(body.find_all(["div", "section", "article", "main", "header", "footer"])):
        element.unwrap()
    return body.decode_contents()


def _assemble_pdf_fpdf2(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Render normalized book HTML with fpdf2 and no system rendering libraries."""
    try:
        fpdf_module = importlib.import_module("fpdf")
        FPDF = getattr(fpdf_module, "FPDF")
        FontFace = getattr(fpdf_module, "FontFace")
    except (ImportError, AttributeError) as error:
        raise ImportError(
            "fpdf2 PDF 输出需要可选依赖，请运行：uv sync --extra pdf-output-lite"
        ) from error

    font_path = _find_fpdf_font()

    class BookPDF(FPDF):
        def footer(self) -> None:
            self.set_y(-12)
            self.set_font("WenyiCJK", size=8)
            self.set_text_color(100)
            self.cell(0, 6, f"{self.page_no()}/{{nb}}", align="C")

    with tempfile.TemporaryDirectory(prefix="trans-novel-fpdf-") as directory:
        html_path = os.path.join(directory, "book.html")
        _assemble_html(
            store,
            source_path,
            html_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
        with open(html_path, encoding="utf-8") as file:
            normalized = _normalize_html_for_fpdf(file.read(), base_dir=directory)

        pdf = BookPDF(format=(148, 210))
        pdf.set_margins(16, 18, 16)
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.add_font("WenyiCJK", style="", fname=font_path)
        pdf.add_font("WenyiCJK", style="B", fname=font_path)
        pdf.add_font("WenyiCJK", style="I", fname=font_path)
        pdf.add_font("WenyiCJK", style="BI", fname=font_path)
        pdf.alias_nb_pages()
        pdf.add_page()
        pdf.write_html(
            normalized,
            font_family="WenyiCJK",
            tag_styles={
                "h1": FontFace(size_pt=20, emphasis="B"),
                "h2": FontFace(size_pt=16, emphasis="B"),
                "h3": FontFace(size_pt=14, emphasis="B"),
                "p": FontFace(size_pt=11),
            },
        )
        pdf.output(out_path)
    return out_path


def _assemble_pdf(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    engine: str,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Dispatch PDF output to the selected rendering backend."""
    if engine == "weasyprint":
        return _assemble_pdf_weasyprint(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if engine == "fpdf2":
        return _assemble_pdf_fpdf2(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    raise ValueError("不支持的 PDF 引擎：" + engine + "（可选 weasyprint / fpdf2）")
