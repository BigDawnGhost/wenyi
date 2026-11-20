"""Generate the About This Translation page at the end of a book."""

from __future__ import annotations

import posixpath
import shutil
import zipfile
from dataclasses import dataclass
from string import Template

from bs4 import BeautifulSoup

from ..atomicio import publish_target
from ..i18n.policy.models import PolicyContext
from ..i18n.policy.resolver import resolve_policy
from ..i18n.resources import read_text

ABOUT_TITLE = "关于此翻译"
ABOUT_ID = "trans-novel-about"
ABOUT_FILENAME = f"{ABOUT_ID}.xhtml"
ABOUT_REPOSITORY = "https://github.com/BigDawnGhost/wenyi"


@dataclass(frozen=True)
class AboutPage:
    """Language settings for an enabled translation about page."""

    lang: str
    locale: str | None = None

    def render(self) -> bytes:
        return about_xhtml(self.lang, locale=self.locale)


def about_xhtml(lang: str, *, locale: str | None = None) -> bytes:
    """Return a standalone XHTML page suitable for the EPUB spine."""
    locale = (
        locale
        or resolve_policy(
            PolicyContext("auto", lang, phase="export", format="epub")
        ).export.about_locale
    )
    return (
        Template(read_text(f"export/about.{locale}.xhtml"))
        # safe_substitute: a literal $ in future template prose must not crash the whole
        # export (audit A-4).
        .safe_substitute(lang=lang, title=ABOUT_TITLE, repository=ABOUT_REPOSITORY)
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


def main_opf_path(archive: zipfile.ZipFile) -> str | None:
    """Find the main OPF named by the container, if the container is present."""
    try:
        return rootfile_path(archive.read("META-INF/container.xml"))
    except KeyError:
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
        item_id = ABOUT_ID
        suffix = 1
        while item_id in existing_ids:
            item_id = f"{ABOUT_ID}-{suffix}"
            suffix += 1

        item = soup.new_tag("item", namespace=manifest.namespace, nsprefix=manifest.prefix)
        item["id"] = item_id
        item["href"] = href
        item["media-type"] = "application/xhtml+xml"
        manifest.append(item)

        itemref = soup.new_tag("itemref", namespace=spine.namespace, nsprefix=spine.prefix)
        itemref["idref"] = item_id
        spine.append(itemref)
        return soup.encode(), True
    except Exception:
        return data, False


def attach_about_entry(
    opf_data: bytes, opf_path: str, existing_names: set[str], about: AboutPage
) -> tuple[bytes, tuple[str, bytes] | None]:
    """Prepare the updated OPF and ZIP member, skipping OPFs that cannot reference the page."""
    entry, href = unique_about_entry(existing_names, opf_path)
    updated_opf, attached = append_about_to_opf(opf_data, href)
    return updated_opf, (entry, about.render()) if attached else None


def append_about_page(epub_path: str, lang: str, *, locale: str | None = None) -> bool:
    """Atomically postprocess an existing EPUB to append the about page to its spine.

    Entries stream from the source archive into the replacement so peak memory stays at
    one entry instead of the whole book (audit A-3). The publish context stays outermost:
    on Windows the replacement needs the source handle closed first.
    """
    with publish_target(epub_path) as tmp_path:
        with zipfile.ZipFile(epub_path, "r") as zin:
            opf_path = main_opf_path(zin)
            names = set(zin.namelist())
            if opf_path is None or opf_path not in names:
                return False

            opf_data, about_entry = attach_about_entry(
                zin.read(opf_path), opf_path, names, AboutPage(lang, locale)
            )
            if about_entry is None:
                return False

            with zipfile.ZipFile(tmp_path, "w") as zout:
                for info in zin.infolist():
                    if info.filename == "mimetype":
                        stored = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                        stored.compress_type = zipfile.ZIP_STORED
                        with zin.open(info) as source, zout.open(stored, "w") as target:
                            shutil.copyfileobj(source, target)
                    elif info.filename == opf_path:
                        zout.writestr(info, opf_data)
                    else:
                        with zin.open(info) as source, zout.open(info, "w") as target:
                            shutil.copyfileobj(source, target)
                zout.writestr(*about_entry)
    return True
