"""生成测试用样本：日文 TXT 与最小 EPUB。"""

from __future__ import annotations

import zipfile

SAMPLE_TXT = """\
# 第一章　出会い

綾小路は教室の窓際に座っていた。空はどこまでも青く、遠くで鳥が鳴いていた。

「おはよう、綾小路くん」と堀北が声をかけた。彼女はいつも通り無表情だった。

綾小路は小さく頷いた。何も言わなかった。

# 第二章　放課後

放課後、二人は屋上で待ち合わせた。風が強かった。

「先輩、これからどうするつもりですか」と堀北が尋ねた。
"""


def write_sample_txt(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(SAMPLE_TXT)


_CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>サンプル小説</dc:title>
    <dc:language>ja</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

_CH1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head>
<body>
<h1>第一章　出会い</h1>
<p>綾小路は教室の窓際に座っていた。</p>
<p>「おはよう、綾小路くん」と堀北が声をかけた。</p>
</body></html>
"""

_CH2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第二章</title></head>
<body>
<h1>第二章　放課後</h1>
<p>放課後、二人は屋上で待ち合わせた。風が強かった。</p>
</body></html>
"""


def write_sample_epub(path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype 必须最先写且不压缩
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", _OPF)
        zf.writestr("OEBPS/ch1.xhtml", _CH1)
        zf.writestr("OEBPS/ch2.xhtml", _CH2)


_NESTED_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Nested</title></head>
<body>
<h1 id="part-1">PART I</h1><p>Part I intro.</p>
<h2 id="section-1">Section 1</h2><p>Section 1 body.</p>
<h1 id="part-2">PART II</h1><p>Part II intro.</p>
<h2 id="section-2">Section 2</h2><p>Section 2 body.</p>
</body></html>
"""

_NESTED_NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
  <navPoint id="part1"><navLabel><text>PART I</text></navLabel>
    <content src="body.xhtml#part-1"/>
    <navPoint id="section1"><navLabel><text>Section 1</text></navLabel>
      <content src="body.xhtml#section-1"/>
    </navPoint>
  </navPoint>
  <navPoint id="part2"><navLabel><text>PART II</text></navLabel>
    <content src="body.xhtml#part-2"/>
    <navPoint id="section2"><navLabel><text>Section 2</text></navLabel>
      <content src="body.xhtml#section-2"/>
    </navPoint>
  </navPoint>
</navMap></ncx>
"""

_FLAT_SECONDARY_NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
  <navPoint><navLabel><text>PART I</text></navLabel><content src="body.xhtml#part-1"/></navPoint>
  <navPoint><navLabel><text>Section 1</text></navLabel><content src="body.xhtml#section-1"/></navPoint>
  <navPoint><navLabel><text>PART II</text></navLabel><content src="body.xhtml#part-2"/></navPoint>
  <navPoint><navLabel><text>Section 2</text></navLabel><content src="body.xhtml#section-2"/></navPoint>
</navMap></ncx>
"""

_NESTED_NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol>
  <li id="part1"><a href="body.xhtml#part-1">PART I</a><ol>
    <li id="section1"><a href="body.xhtml#section-1">Section 1</a></li>
  </ol></li>
  <li id="part2"><a href="body.xhtml#part-2">PART II</a><ol>
    <li id="section2"><a href="body.xhtml#section-2">Section 2</a></li>
  </ol></li>
</ol></nav></body></html>
"""


def write_nested_toc_epub(
    path: str,
    *,
    toc_kind: str = "ncx",
    broken_part2_fragment: bool = False,
    nav_in_spine: bool = False,
    empty_title_page: bool = False,
    ncx_filename: str = "toc.ncx",
) -> None:
    """生成“同一 XHTML 内两个顶层章 + 两个子标题”的 EPUB。"""
    if toc_kind not in {"ncx", "nav", "both"}:
        raise ValueError(toc_kind)
    toc_item = (
        f'<item id="toc" href="{ncx_filename}" media-type="application/x-dtbncx+xml"/>'
        if toc_kind == "ncx"
        else (
            '<item id="toc" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            if toc_kind == "nav"
            else (
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                f'<item id="toc" href="{ncx_filename}" media-type="application/x-dtbncx+xml"/>'
            )
        )
    )
    spine_attr = ' toc="toc"' if toc_kind in {"ncx", "both"} else ""
    nav_spine_id = "toc" if toc_kind == "nav" else "nav"
    nav_itemref = (
        f'<itemref idref="{nav_spine_id}"/>'
        if nav_in_spine and toc_kind in {"nav", "both"}
        else ""
    )
    title_item = (
        '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>'
        if empty_title_page
        else ""
    )
    title_itemref = '<itemref idref="title"/>' if empty_title_page else ""
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Nested</dc:title></metadata>
<manifest>{toc_item}{title_item}<item id="body" href="body.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine{spine_attr}>{nav_itemref}{title_itemref}<itemref idref="body"/></spine></package>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/body.xhtml", _NESTED_BODY)
        ncx = _NESTED_NCX.replace("#part-2", "#missing") if broken_part2_fragment else _NESTED_NCX
        nav = _NESTED_NAV.replace("#part-2", "#missing") if broken_part2_fragment else _NESTED_NAV
        if empty_title_page:
            ncx = ncx.replace(
                "<navMap>",
                "<navMap><navPoint><navLabel><text>Title Page</text></navLabel>"
                '<content src="title.xhtml"/></navPoint>',
            )
            nav = nav.replace(
                "<nav epub:type=\"toc\"><ol>",
                '<nav epub:type="toc"><ol><li><a href="title.xhtml">Title Page</a></li>',
            )
            zf.writestr("OEBPS/title.xhtml", "<html><body><div class=\"cover\"></div></body></html>")
        if toc_kind == "ncx":
            zf.writestr(f"OEBPS/{ncx_filename}", ncx)
        elif toc_kind == "nav":
            zf.writestr("OEBPS/nav.xhtml", nav)
        else:
            zf.writestr("OEBPS/nav.xhtml", nav)
            zf.writestr(f"OEBPS/{ncx_filename}", _FLAT_SECONDARY_NCX)


def write_grouped_nav_epub(path: str) -> None:
    """生成用无 href ``span`` 表示顶层分部的 EPUB3 NAV。"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Grouped</dc:title></metadata>
<manifest>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
</manifest><spine><itemref idref="body"/></spine></package>"""
    nav = """<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>
 <li><span>PART I</span><ol><li><a href="body.xhtml#section-1">Section 1</a></li></ol></li>
 <li><span>PART II</span><ol><li><a href="body.xhtml#section-2">Section 2</a></li></ol></li>
</ol></nav></body></html>"""
    body = """<html><body>
<h2 id="section-1">Section 1</h2><p>One.</p>
<h2 id="section-2">Section 2</h2><p>Two.</p>
</body></html>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/body.xhtml", body)


def write_cross_resource_toc_epub(path: str) -> None:
    """生成第一个逻辑章横跨两个 spine XHTML 的 EPUB2 样本。"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Cross</dc:title></metadata>
<manifest>
  <item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  <item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>
  <item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>
  <item id="three" href="three.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine toc="toc"><itemref idref="one"/><itemref idref="two"/><itemref idref="three"/></spine>
</package>"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
  <navPoint><navLabel><text>PART I</text></navLabel><content src="one.xhtml#part-1"/>
    <navPoint><navLabel><text>Section 1</text></navLabel><content src="two.xhtml#section-1"/></navPoint>
  </navPoint>
  <navPoint><navLabel><text>PART II</text></navLabel><content src="three.xhtml#part-2"/></navPoint>
</navMap></ncx>"""
    resources = {
        "one.xhtml": '<html><body><h1 id="part-1">PART I</h1><p>One.</p></body></html>',
        "two.xhtml": '<html><body><h2 id="section-1">Section 1</h2><p>Two.</p></body></html>',
        "three.xhtml": '<html><body><h1 id="part-2">PART II</h1><p>Three.</p></body></html>',
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        for name, content in resources.items():
            zf.writestr(f"OEBPS/{name}", content)


_INLINE_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>内联插图样本</dc:title>
    <dc:language>fr</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="image" href="image.jpg" media-type="image/jpeg"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>
"""

_INLINE_CH1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapitre I</title></head>
<body>
<h1>Chapitre I</h1>
<p class="Textbody"><img src="image.jpg"/><span id="kobo.1.1">Je suis là, sous le pommier.</span></p>
</body></html>
"""


def write_inline_sample_epub(path: str) -> None:
    """生成与《小王子》相同的“段首图片 + 句子”结构。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr("OEBPS/content.opf", _INLINE_OPF)
        zf.writestr("OEBPS/ch1.xhtml", _INLINE_CH1)
        zf.writestr("OEBPS/image.jpg", b"inline-image")
