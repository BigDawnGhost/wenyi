"""Generate the About This Translation page at the end of a book."""

from __future__ import annotations

import os
import posixpath
import zipfile
from string import Template

from bs4 import BeautifulSoup

from ..i18n.languages import normalize_language
from ..i18n.resources import read_text

ABOUT_TITLE = "关于此翻译"
ABOUT_FILENAME = "trans-novel-about.xhtml"
ABOUT_REPOSITORY = "https://github.com/BigDawnGhost/wenyi"


def about_xhtml(lang: str) -> bytes:
    """Return a standalone XHTML page suitable for the EPUB spine."""
    locale = "zh" if normalize_language(lang) == "zh" else "en"
    return (
        Template(read_text(f"export/about.{locale}.xhtml"))
        .substitute(lang=lang, title=ABOUT_TITLE, repository=ABOUT_REPOSITORY)
        .encode("utf-8")
    )


def rootfile_path(container_xml: bytes) -> str | None:
    """Read the main OPF path from EPUB container.xml."""
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
    """Return a ZIP path and OPF-relative href that do not collide with original resources."""
    opf_dir = posixpath.dirname(opf_path)
    stem, ext = posixpath.splitext(ABOUT_FILENAME)
    suffix = 0
    while True:
        filename = ABOUT_FILENAME if suffix == 0 else f"{stem}-{suffix}{ext}"
        entry = posixpath.join(opf_dir, filename) if opf_dir else filename
        if entry not in existing_names:
            return entry, filename
        suffix += 1


def append_about_to_opf(data: bytes, href: str) -> tuple[bytes, bool]:
    """Add the about page to the OPF manifest and spine; report whether insertion succeeded."""
    try:
        soup = BeautifulSoup(data, "xml")
        manifest = soup.find("manifest")
        spine = soup.find("spine")
        if manifest is None or spine is None:
            return data, False

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
        return soup.encode(), True
    except Exception:
        return data, False


def append_about_page(epub_path: str, lang: str) -> bool:
    """Atomically postprocess an existing EPUB to append the about page to its spine."""
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
        opf_data, attached = append_about_to_opf(entries[opf_path], about_href)
        if not attached:
            return False
        entries[opf_path] = opf_data

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
