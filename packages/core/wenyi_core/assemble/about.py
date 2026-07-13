"""生成书末“关于此翻译”说明页。"""

from __future__ import annotations

import os
import posixpath
import zipfile

from bs4 import BeautifulSoup

ABOUT_TITLE = "关于此翻译"
ABOUT_FILENAME = "trans-novel-about.xhtml"
ABOUT_REPOSITORY = "https://github.com/BigDawnGhost/wenyi"

def about_xhtml(lang: str) -> bytes:
    """返回可独立加入 EPUB spine 的 XHTML 页面。"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">
<head>
  <title>{ABOUT_TITLE}</title>
  <style>
    html, body {{ writing-mode: horizontal-tb; direction: ltr; }}
    body {{ margin: 8% 8%; font-family: serif; line-height: 1.65; }}
    .tn-about {{ max-width: 42em; margin: 0 auto; }}
    h1 {{ margin: 0 0 1.8em; text-align: center; font-size: 1.7em; }}
    p {{ margin: 0 0 1.5em; text-align: justify; }}
    .tn-about-lead, .tn-about-closing {{ text-align: center; }}
    .tn-about-feedback {{ margin: 1.8em 0; font-size: 1.05em; }}
    .tn-about-feedback p {{ text-align: left; }}
    a {{ color: #1756d1; text-decoration: underline; }}
  </style>
</head>
<body>
  <section class="tn-about">
    <h1>{ABOUT_TITLE}</h1>
    <p class="tn-about-lead">本书由 <strong>文译（Wenyi）</strong> 项目生成。</p>
    <p>文译是一个开源的命令行工具，致力于将多语言 EPUB、FB2、TXT 小说翻译为中文。它以长篇小说的翻译质量为重点，支持全书预扫、滚动上下文、实时术语库、润色和审校等功能，力求在准确的前提下让译文从可读向好读迈进。</p>
    <p>项目目前仍在初期阶段，翻译质量尚有提升空间。如果您发现译文中的问题（如专有名词翻译不一致、口头禅前后不统一等），或对翻译质量有任何建议，欢迎通过以下方式反馈：</p>
    <div class="tn-about-feedback">
      <p><strong>GitHub 仓库：</strong>
        <a href="{ABOUT_REPOSITORY}">github.com/BigDawnGhost/wenyi</a><br/>
        提交 Issue，或在讨论区提出您的想法<br/>
        如果您具备编程能力，欢迎提交 Pull Request，共同改进这个项目<br/>
        项目交流 QQ 群：1055065098
      </p>
    </div>
    <p class="tn-about-closing">本项目为个人兴趣所开发，仅在于针对长文本书籍的译介做出一份微薄的努力。每一份反馈和建议，都会让这个工具变得更好。感谢您的阅读。</p>
  </section>
</body>
</html>
""".encode("utf-8")


def rootfile_path(container_xml: bytes) -> str | None:
    """从 EPUB container.xml 读取主 OPF 路径。"""
    try:
        soup = BeautifulSoup(container_xml, "xml")
        rootfile = soup.find("rootfile")
        if rootfile is None:
            return None
        path = rootfile.get("full-path")
        return path if isinstance(path, str) and path else None
    except Exception:
        return None


def unique_about_entry(existing_names: set[str], opf_path: str) -> tuple[str, str]:
    """返回不与原书资源冲突的（zip 内路径, OPF 相对 href）。"""
    opf_dir = posixpath.dirname(opf_path)
    stem, ext = posixpath.splitext(ABOUT_FILENAME)
    suffix = 0
    while True:
        filename = ABOUT_FILENAME if suffix == 0 else f"{stem}-{suffix}{ext}"
        entry = posixpath.join(opf_dir, filename) if opf_dir else filename
        if entry not in existing_names:
            return entry, filename
        suffix += 1


def append_about_to_opf(data: bytes, href: str) -> bytes:
    """把说明页加入 OPF manifest，并作为 spine 最后一项。"""
    try:
        soup = BeautifulSoup(data, "xml")
        manifest = soup.find("manifest")
        spine = soup.find("spine")
        if manifest is None or spine is None:
            return data

        existing_ids: set[str] = set()
        for existing_item in manifest.find_all("item"):
            value = existing_item.get("id")
            if isinstance(value, str):
                existing_ids.add(value)
        item_id = "trans-novel-about"
        suffix = 1
        while item_id in existing_ids:
            item_id = f"trans-novel-about-{suffix}"
            suffix += 1

        item = soup.new_tag("item")
        item["id"] = item_id
        item["href"] = href
        item["media-type"] = "application/xhtml+xml"
        manifest.append(item)

        itemref = soup.new_tag("itemref")
        itemref["idref"] = item_id
        spine.append(itemref)
        return soup.encode()
    except Exception:
        return data


def append_about_page(epub_path: str, lang: str) -> bool:
    """对已经生成的 EPUB 做一次原子后处理，将说明页追加到 spine 末尾。"""
    with zipfile.ZipFile(epub_path, "r") as zin:
        try:
            opf_path = rootfile_path(zin.read("META-INF/container.xml"))
        except KeyError:
            return False
        if not opf_path or opf_path not in zin.namelist():
            return False

        infos = zin.infolist()
        entries = {info.filename: zin.read(info.filename) for info in infos}
        about_entry, about_href = unique_about_entry(set(entries), opf_path)
        entries[opf_path] = append_about_to_opf(entries[opf_path], about_href)

    tmp_path = epub_path + ".about.tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for info in infos:
                data = entries[info.filename]
                if info.filename == "mimetype":
                    zout.writestr(info, data, zipfile.ZIP_STORED)
                else:
                    zout.writestr(info, data)
            zout.writestr(about_entry, about_xhtml(lang))
        os.replace(tmp_path, epub_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return True
