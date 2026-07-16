"""FB2 (FictionBook) 读取器。

FB2 即一种 XML 格式（.fb2），常见命名空间为
http://www.gribuser.ru/xml/fictionbook/2.0；部分文件使用 2.1 或省略命名空间。

结构：
  <FictionBook>
    <description><title-info>…</title-info></description>
    <body>                           ← 正文
      <section>                      ← 一章（可嵌套子 section：部 → 章）
        <title><p>章标题</p></title>
        <subtitle>小标题</subtitle>
        <p>正文段落…</p>
        <epigraph>…</epigraph> <cite>…</cite> <poem><stanza><v>诗行</v></stanza></poem>
        <empty-line/>
      </section>
      …
    </body>
    <body name="notes">…</body>       ← 注释，跳过

- 嵌套 section 递归展平为扁平章列表（_walk_sections），不丢子章正文。
- 正文块覆盖 p / subtitle / epigraph / cite / poem(stanza/v) / text-author，避免诗歌引文丢字。
- 回填：FB2 不回填原始文件（无锚点机制），assemble 走通用 EPUB 生成。
"""

from __future__ import annotations

import base64
import os
import re
import xml.etree.ElementTree as ET

from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

def _local(el: ET.Element) -> str:
    """返回 FB2 元素去除 XML 命名空间后的标签名。"""
    return el.tag.rsplit("}", 1)[-1]


def _strip_markup(el: ET.Element) -> str:
    """提取元素内的纯文本，保留基本空白。"""
    parts: list[str] = []
    for text in el.itertext():
        if text:
            parts.append(text)
    return "".join(parts)


# 容器型块：本身无文字，需下钻其子元素（poem 的 stanza/title、cite 的 p 等）
_CONTAINER_BLOCKS = {"epigraph", "cite", "poem", "stanza", "title", "annotation"}


def _image_id(el: ET.Element) -> str:
    """Return the referenced FB2 binary id from an xlink/plain href."""
    for name, value in el.attrib.items():
        if name.rsplit("}", 1)[-1] == "href":
            return value.removeprefix("#").strip()
    return ""


def _direct_segments(
    section: ET.Element,
    chapter_index: int,
) -> tuple[str, list[Segment], list[dict[str, int | str]]]:
    """提取本 <section> 的【直接】内容，不下钻子 <section>。

    覆盖 p / subtitle / epigraph / cite / poem(stanza/v) / text-author 等正文块，
    避免诗歌、引文、小标题被丢字。图片记录其相对段落位置，导出时再从原 FB2
    的 binary 资源恢复。返回 (标题文本, segments, images)；标题作为 heading 排首。
    """
    segments: list[Segment] = []
    images: list[dict[str, int | str]] = []
    idx = 0
    title_text = ""

    def add(text: str, kind: str) -> None:
        """清洗并追加非空段落，同时分配稳定的章内索引和锚点。"""
        nonlocal idx
        text = text.strip()
        if text:
            segments.append(
                Segment(index=idx, source=text, kind=kind,
                        anchor=f"tn{chapter_index}_{idx}")
            )
            idx += 1

    def emit_block(el: ET.Element) -> None:
        """递归展开正文容器，收集文字块和图片相对位置。"""
        tag = _local(el)
        if tag == "image":
            image_id = _image_id(el)
            if image_id:
                images.append({"id": image_id, "position": len(segments)})
        elif tag == "subtitle":
            add(_strip_markup(el), KIND_HEADING)   # 节内小标题
        elif tag in ("p", "v", "text-author"):     # 段落 / 诗行 / 署名
            for image in el.iter():
                if image is not el and _local(image) == "image":
                    emit_block(image)
            add(_strip_markup(el), KIND_TEXT)
        elif tag in _CONTAINER_BLOCKS:             # 容器：下钻
            for sub in el:
                emit_block(sub)
        # empty-line / 其它 → 跳过

    for child in section:
        tag = _local(child)
        if tag == "section":
            continue  # 子节由 _walk_sections 递归处理
        if tag == "title":
            title_text = _strip_markup(child).strip()
            add(title_text, KIND_HEADING)
        else:
            emit_block(child)
    return title_text, segments, images


def _walk_sections(section: ET.Element, chapters: list[Chapter]) -> None:
    """递归遍历 <section>：叶子节成一章；含子节者保留自身正文/部标题后再下钻。

    FB2 常见“部 → 章”层级（section 嵌套 section）；只取直接子节内容会丢正文，
    故此处递归展开为扁平章列表，确保无损。
    """
    ci = len(chapters)
    title_text, segs, images = _direct_segments(section, ci)
    child_sections = [c for c in section if _local(c) == "section"]

    if child_sections:
        # 容器节：若有自身正文（标题之外的段落）或仅有部标题，都先成一章保留，避免丢失
        has_body = any(s.kind == KIND_TEXT for s in segs)
        if has_body or title_text:
            chapters.append(_make_chapter(ci, title_text, segs, images))
        for cs in child_sections:
            _walk_sections(cs, chapters)
    elif segs:
        chapters.append(_make_chapter(ci, title_text, segs, images))


def _make_chapter(
    ci: int,
    title_text: str,
    segments: list[Segment],
    images: list[dict[str, int | str]],
) -> Chapter:
    """用提取结果构造章节，并为无标题章节生成可展示标题。"""
    if not title_text and segments:
        title_text = segments[0].source[:80]
    elif not title_text:
        title_text = f"第{ci + 1}章"
    meta = {"fb2_images": images} if images else {}
    return Chapter(index=ci, title=title_text, segments=segments, meta=meta)


def _body_title_chapter(body: ET.Element) -> Chapter | None:
    """把正文 ``<body><title>`` 解析为独立的标题页章节。"""
    title_el = next((child for child in body if _local(child) == "title"), None)
    if title_el is None:
        return None

    lines = [
        _strip_markup(child).strip()
        for child in title_el
        if _local(child) == "p" and _strip_markup(child).strip()
    ]
    if not lines:
        return None

    segments = [
        Segment(
            index=idx,
            source=line,
            kind=KIND_HEADING,
            anchor=f"tn0_{idx}",
        )
        for idx, line in enumerate(lines)
    ]
    return Chapter(index=0, title=lines[-1], segments=segments)


def read_fb2(path: str, source_lang: str, target_lang: str) -> Document:
    """读取 .fb2 文件并返回 Document。"""
    with open(path, "rb") as f:
        raw = f.read()

    # 剥离 XML 声明中的 encoding（FB2 常见 windows-1251）
    enc = "utf-8"
    m = re.search(rb"<\?xml.*?encoding\s*=\s*['\"]([^'\"]+)['\"]", raw)
    if m:
        enc = m.group(1).decode("ascii", errors="replace")
    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode("utf-8", errors="replace")

    root = ET.fromstring(text)

    resources: list[dict[str, str]] = []
    for binary in root.iter():
        if _local(binary) != "binary":
            continue
        resource_id = binary.attrib.get("id", "").strip()
        if resource_id:
            resources.append(
                {
                    "id": resource_id,
                    "content_type": binary.attrib.get(
                        "content-type", "application/octet-stream"
                    ),
                }
            )

    cover_image = ""
    for coverpage in root.iter():
        if _local(coverpage) != "coverpage":
            continue
        image = next(
            (element for element in coverpage.iter() if _local(element) == "image"),
            None,
        )
        if image is not None:
            cover_image = _image_id(image)
        break

    # ── 书名 ──
    title = os.path.splitext(os.path.basename(path))[0]
    for desc in root.iter():
        if _local(desc) != "title-info":
            continue
        for child in desc:
            if _local(child) == "book-title":
                if child.text:
                    title = child.text.strip()
                break

    # ── 章节 ──
    chapters: list[Chapter] = []
    # 只取第一个正文 <body>，跳过 body[name="notes"] 等附属 body
    for body in root:
        if _local(body) != "body":
            continue
        body_name = body.attrib.get("name", "")
        if body_name:  # notes, comments 等附属 body
            continue
        title_chapter = _body_title_chapter(body)
        if title_chapter is not None:
            chapters.append(title_chapter)
        for section in body:
            if _local(section) == "section":
                _walk_sections(section, chapters)
        break  # 只处理第一个 body

    if not chapters:
        # 兜底：整篇当作一章
        segments: list[Segment] = []
        idx = 0
        for p in root.iter():
            if _local(p) != "p":
                continue
            text = _strip_markup(p).strip()
            if text:
                segments.append(
                    Segment(
                        index=idx,
                        source=text,
                        kind=KIND_TEXT,
                        anchor=f"tn0_{idx}",
                    )
                )
                idx += 1
        if segments:
            chapters.append(Chapter(index=0, title=title, segments=segments))

    meta: dict[str, object] = {}
    if resources:
        meta["fb2_resources"] = resources
    if cover_image:
        meta["fb2_cover_image"] = cover_image

    return Document(
        title=title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="fb2",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta=meta,
    )


def read_fb2_binaries(path: str) -> dict[str, tuple[str, bytes]]:
    """Read embedded FB2 binaries for export without persisting them in run state."""
    with open(path, "rb") as file:
        root = ET.fromstring(file.read())

    resources: dict[str, tuple[str, bytes]] = {}
    for binary in root.iter():
        if _local(binary) != "binary":
            continue
        resource_id = binary.attrib.get("id", "").strip()
        encoded = "".join((binary.text or "").split())
        if not resource_id or not encoded:
            continue
        try:
            payload = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError):
            continue
        resources[resource_id] = (
            binary.attrib.get("content-type", "application/octet-stream"),
            payload,
        )
    return resources
