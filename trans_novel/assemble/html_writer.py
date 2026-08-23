"""独立 HTML 文件输出。

读取 manifest 和章节数据，判断是否存在可回填的 HTML/PDF 模板。
有模板时使用 html_renderer 回填章节节点，通过 html_resources 解析和物化资源；
无模板时按当前结构重建标题、段落和双语内容。
"""

from __future__ import annotations

import zipfile
from html import escape

from bs4 import BeautifulSoup
from bs4.element import Tag

from ..ingest.models import KIND_HEADING
from ..pipeline.runstore import RunStore
from .epub_writer import _epub_resource_specs, _render_epub_resources
from .html_renderer import (
    _BILINGUAL_CSS,
    _BILINGUAL_STYLE_ID,
    _render_chapter_html,
)
from .html_resources import _materialize_html_resources, _template_resource_source
from .writer_common import _bilingual_source, _epub_lang, _merged_paragraphs


def _assemble_html(
    store: RunStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """回填 HTML 原文：逐章渲染 template，拼接为完整 HTML 输出。"""
    m = store.load_manifest()
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_head_html = meta.get("head_html", "")
    head_html = raw_head_html if isinstance(raw_head_html, str) else ""
    # 始终确保 charset 声明，否则浏览器无法正确识别编码导致中文乱码
    if "charset" not in head_html.replace(" ", "").lower():
        head_html = '<meta charset="utf-8"/>\n' + head_html
    if bilingual and not preserve_source_style and _BILINGUAL_STYLE_ID not in head_html:
        head_html += f'<style id="{_BILINGUAL_STYLE_ID}">\n{_BILINGUAL_CSS}</style>'

    body_parts: list[str] = []
    rendered_epub = False
    if m.get("fmt") == "epub" and _epub_resource_specs(meta):
        chapters = [store.load_chapter(c["index"]) for c in m["chapters"]]
        with zipfile.ZipFile(source_path, "r") as archive:
            rendered = _render_epub_resources(
                archive,
                chapters,
                meta,
                book_title=m.get("title", "") if isinstance(m.get("title"), str) else "",
                bilingual=bilingual,
                order=order,
                preserve_source_style=preserve_source_style,
                source_lang=(
                    m.get("source_lang", "") if isinstance(m.get("source_lang"), str) else ""
                ),
            )
        for _resource_index, href in _epub_resource_specs(meta):
            resource_html = rendered.get(href)
            if not resource_html:
                continue
            resource_soup = BeautifulSoup(resource_html, "html.parser")
            resource_body = resource_soup.find("body")
            body_parts.append(
                resource_body.decode_contents()
                if isinstance(resource_body, Tag)
                else str(resource_soup)
            )
        rendered_epub = bool(body_parts)

    for c in [] if rendered_epub else m["chapters"]:
        ch = store.load_chapter(c["index"])
        if ch.template:
            # 复用 EPUB 的章节渲染（替换 data-tn-id → 译文，处理 cont 续段与双语）
            body_parts.append(
                _render_chapter_html(
                    ch,
                    bilingual=bilingual,
                    order=order,
                    preserve_source_style=preserve_source_style,
                )
            )
            continue

        # TXT / Markdown 等无 HTML 模板的输入也必须能导出正文。
        for kind, target, source in _merged_paragraphs(ch):
            if kind == KIND_HEADING:
                level = ch.meta.get("heading_level", 1)
                level = level if isinstance(level, int) and 1 <= level <= 6 else 1
                target_html = f"<h{level}>{escape(target)}</h{level}>"
            else:
                target_html = f"<p>{escape(target)}</p>"
            src = _bilingual_source(source, target) if (bilingual and kind != KIND_HEADING) else ""
            if not src:
                body_parts.append(target_html)
                continue
            source_html = f'<p class="tn-source">{escape(src)}</p>'
            if order == "source_first":
                body_parts.extend((source_html, target_html))
            else:
                body_parts.extend((target_html, source_html))

    full_html = f"""<!DOCTYPE html>
<html lang="{escape(_epub_lang(m.get("target_lang", "zh")))}">
<head>
{head_html}
</head>
<body>
{"".join(body_parts)}
</body>
</html>"""
    full_html = _materialize_html_resources(
        full_html,
        source_path=_template_resource_source(store, m, source_path),
        out_path=out_path,
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    return out_path
