"""回填：把译文写回原格式。

- 纯文本：按章重建，标题 + 段落（空行分隔）。
- EPUB：重开原始 zip，逐条目原样拷贝；命中章节 href 的 XHTML 用 chapter.template
  按 data-tn-id 锚点替换为译文后写回，非正文资源（图片/CSS/字体）不动。
缺失译文的段回退使用原文，保证不丢内容。
"""

from __future__ import annotations

import os
import zipfile

from bs4 import BeautifulSoup

from ..ingest.models import Chapter, KIND_HEADING
from ..pipeline.runstore import RunStore


def _default_out(source_path: str, out_format: str) -> str:
    base, _ = os.path.splitext(source_path)
    ext = ".epub" if out_format == "epub" else ".txt"
    return f"{base}.zh{ext}"


def _seg_text(seg) -> str:
    return seg.target if (seg.target and seg.target.strip()) else seg.source


# ── 纯文本 ──────────────────────────────────────────────────────────────────
def _assemble_text(store: RunStore, out_path: str) -> str:
    m = store.load_manifest()
    chapter_blocks: list[str] = []
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        paras = [_seg_text(s) for s in ch.segments if s.source.strip()]
        chapter_blocks.append("\n\n".join(paras))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(chapter_blocks) + "\n")
    return out_path


# ── EPUB ────────────────────────────────────────────────────────────────────
def _render_chapter_html(chapter: Chapter) -> str:
    soup = BeautifulSoup(chapter.template or "", "html.parser")
    by_anchor = {s.anchor: _seg_text(s) for s in chapter.segments if s.anchor}
    for anchor, text in by_anchor.items():
        el = soup.find(attrs={"data-tn-id": anchor})
        if el is None:
            continue
        el.clear()
        el.append(text)
        del el["data-tn-id"]
    return str(soup)


def _assemble_epub(store: RunStore, source_path: str, out_path: str) -> str:
    m = store.load_manifest()
    # href -> 渲染后的 XHTML
    rendered: dict[str, str] = {}
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        if ch.href and ch.template:
            rendered[ch.href] = _render_chapter_html(ch)

    with zipfile.ZipFile(source_path, "r") as zin:
        infos = zin.infolist()
        with zipfile.ZipFile(out_path, "w") as zout:
            for info in infos:
                name = info.filename
                if name in rendered:
                    zout.writestr(info, rendered[name].encode("utf-8"))
                elif name == "mimetype":
                    zout.writestr(info, zin.read(name), zipfile.ZIP_STORED)
                else:
                    zout.writestr(info, zin.read(name))
    return out_path


def _build_epub_from_chapters(store: RunStore, out_path: str) -> str:
    """从章节数据生成一个规范的 EPUB（用于纯文本输入）。"""
    from html import escape

    m = store.load_manifest()
    title = m.get("title", "translated")
    lang = m.get("target_lang", "zh")

    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n'
    )

    chapter_files: list[tuple[str, str]] = []  # (filename, xhtml)
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        body_parts = []
        for s in ch.segments:
            if not s.source.strip():
                continue
            text = escape(_seg_text(s))
            tag = "h1" if s.kind == KIND_HEADING else "p"
            body_parts.append(f"<{tag}>{text}</{tag}>")
        fname = f"ch{c['index']}.xhtml"
        xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">\n'
            f"<head><title>{escape(ch.title)}</title></head>\n<body>\n"
            + "\n".join(body_parts)
            + "\n</body></html>\n"
        )
        chapter_files.append((fname, xhtml))

    manifest_items = "\n".join(
        f'    <item id="ch{i}" href="{fn}" media-type="application/xhtml+xml"/>'
        for i, (fn, _) in enumerate(chapter_files)
    )
    spine_items = "\n".join(f'    <itemref idref="ch{i}"/>' for i in range(len(chapter_files)))
    nav_li = "\n".join(
        f'      <li><a href="{fn}">{escape(store.load_chapter(c["index"]).title)}</a></li>'
        for (fn, _), c in zip(chapter_files, m["chapters"])
    )
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{escape(title)}</dc:title>\n"
        f"    <dc:language>{lang}</dc:language>\n"
        f'    <dc:identifier id="bookid">trans-novel-{escape(title)}</dc:identifier>\n'
        "  </metadata>\n  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        f"{manifest_items}\n  </manifest>\n  <spine>\n{spine_items}\n  </spine>\n</package>\n"
    )
    nav = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        "<head><title>目录</title></head>\n<body>\n"
        '  <nav epub:type="toc" id="toc"><h1>目录</h1>\n    <ol>\n'
        f"{nav_li}\n    </ol>\n  </nav>\n</body></html>\n"
    )

    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        for fn, xhtml in chapter_files:
            z.writestr(f"OEBPS/{fn}", xhtml)
    return out_path


def assemble(store: RunStore, source_path: str, out_path: str | None = None,
             out_format: str = "epub") -> str:
    """生成译文文件（默认 EPUB）。

    out_format="epub"（默认）：
      - 原文是 EPUB → 按原模板回填，保留排版/资源；
      - 原文是纯文本 → 生成一个规范的 EPUB（标题 h1 + 段落 p）。
    out_format="txt"：无论原文格式，按章重建为纯文本。
    """
    m = store.load_manifest()
    if out_format == "txt":
        return _assemble_text(store, out_path or _default_out(source_path, "txt"))
    # epub
    out_path = out_path or _default_out(source_path, "epub")
    if m["fmt"] == "epub":
        return _assemble_epub(store, source_path, out_path)
    return _build_epub_from_chapters(store, out_path)
