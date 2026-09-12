"""Finalize language metadata in generated EPUB navigation."""

from __future__ import annotations

import os
import zipfile

from lxml import etree

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def annotate_generated_navigation(
    out_path: str, chapter_languages: dict[str, str], book_lang: str
) -> None:
    localized = {name: lang for name, lang in chapter_languages.items() if lang != book_lang}
    if not localized:
        return
    with zipfile.ZipFile(out_path, "r") as source:
        infos = source.infolist()
        entries = {info.filename: source.read(info.filename) for info in infos}
    changed = False
    parser = etree.XMLParser(no_network=True, recover=False, resolve_entities=False)
    for name, data in entries.items():
        lowered = name.lower()
        if not lowered.endswith(("nav.xhtml", "nav.html", ".ncx")):
            continue
        try:
            root = etree.fromstring(data, parser)
        except etree.XMLSyntaxError:
            continue
        resource_changed = False
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            local = node.tag.rsplit("}", 1)[-1].lower()
            href = (
                node.get("href")
                if local == "a"
                else node.get("src")
                if local == "content"
                else None
            )
            if not href:
                continue
            lang = localized.get(os.path.basename(href.split("#", 1)[0]))
            if not lang:
                continue
            owner = node if local == "a" else node.getparent()
            if owner is not None:
                owner.set("lang", lang)
                owner.set(_XML_LANG, lang)
                resource_changed = True
                changed = True
        if resource_changed:
            entries[name] = etree.tostring(
                root.getroottree(), encoding="UTF-8", xml_declaration=True
            )
    if not changed:
        return
    temporary = out_path + ".lang.tmp"
    try:
        with zipfile.ZipFile(temporary, "w") as output:
            for info in infos:
                output.writestr(info, entries[info.filename])
        os.replace(temporary, out_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
