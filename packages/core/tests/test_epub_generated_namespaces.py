"""Exercise generated EPUB namespaces through the public writers and an independent XML parser."""

from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from wenyi_core.assemble.epub_package import EpubChapter, EpubTocEntry, write_epub
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.storage.file import FileStorage

XHTML = "http://www.w3.org/1999/xhtml"
SVG = "http://www.w3.org/2000/svg"
MATH = "http://www.w3.org/1998/Math/MathML"
XLINK = "http://www.w3.org/1999/xlink"
OPF = "http://www.idpf.org/2007/opf"
NS = {"h": XHTML, "s": SVG, "m": MATH, "f": "urn:foreign", "o": OPF}


def _write(path, content):
    write_epub(
        path,
        title="Book",
        lang="zh",
        chapters=[EpubChapter("ch0.xhtml", "New title", content)],
        toc=[EpubTocEntry("ch0.xhtml", "New title")],
        resources=[],
        styled_bilingual=True,
    )


def _read(path):
    with ZipFile(path) as archive:
        chapter = archive.read("EPUB/ch0.xhtml")
        opf = ET.fromstring(archive.read("EPUB/content.opf"))
    item = opf.find("o:manifest/o:item[@href='ch0.xhtml']", NS)
    assert item is not None
    return chapter, set(item.get("properties", "").split())


@pytest.mark.parametrize(
    "head",
    ["", '<x:head xmlns="urn:foreign"><title>Keep</title><x:title>Replace</x:title></x:head>'],
)
def test_prefixed_shell_uses_expanded_names(tmp_path, head):
    content = (
        f'<x:html xmlns:x="{XHTML}" xmlns="urn:foreign">'
        f"{head}<x:body><p>Foreign</p><x:p>HTML</x:p></x:body></x:html>"
    )
    out = tmp_path / "prefixed.epub"
    _write(out, content)
    raw, properties = _read(out)
    root = ET.fromstring(raw)
    assert root.tag == f"{{{XHTML}}}html"
    assert root.findall("h:head/h:title", NS)[0].text == "New title"
    assert len(root.findall("h:head/h:title", NS)) == 1
    assert root.find("h:head/h:style[@id='tn-bilingual-style']", NS) is not None
    assert root.findtext("h:head/f:title", namespaces=NS) == ("Keep" if head else None)
    assert root.findtext("h:body/f:p", namespaces=NS) == "Foreign"
    assert root.findtext("h:body/h:p", namespaces=NS) == "HTML"
    assert not properties


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('<html xmlns="urn:foreign"><body/></html>', "complete HTML documents"),
        (f'<h:html xmlns:h="{XHTML}"><body/></h:html>', "contain a body"),
    ],
)
def test_foreign_shell_names_do_not_count_as_html(tmp_path, content, message):
    with pytest.raises(ValueError, match=message):
        _write(tmp_path / "not-html.epub", content)


def test_namespace_rebinding_and_default_undeclaration_round_trip(tmp_path):
    content = (
        f'<html xmlns="{XHTML}" xmlns:m="{MATH}" xmlns:s="{SVG}" xmlns:ref="{XLINK}"'
        ' xmlns:xlink="urn:foreign"><body>'
        f'<svg xmlns="{SVG}" viewBox="0 0 2 2"><s:image href="#local"'
        ' xlink:href="#other" ref:href="https://example.invalid/example.png"/>'
        f'<foreignObject><p xmlns="{XHTML}">Intro <m:math><m:mi>x</m:mi></m:math></p>'
        '<math xmlns="urn:foreign"><node/></math>'
        '<plain xmlns=""><svg/></plain></foreignObject></svg>'
        '<m:math xmlns:m="urn:foreign"><m:mi/></m:math><m:math><m:mi>y</m:mi></m:math>'
        '<other xmlns="urn:foreign" xmlns:f="urn:foreign" f:flag="yes" flag="no"/>'
        "</body></html>"
    )
    out = tmp_path / "rebinding.epub"
    for _ in range(2):
        _write(out, content)
        raw, properties = _read(out)
        root = ET.fromstring(raw)
        assert properties == {"svg", "mathml", "remote-resources"}
        svg = root.find("h:body/s:svg", NS)
        assert svg is not None and svg.get("viewBox") == "0 0 2 2"
        image = svg.find("s:image", NS)
        assert image is not None
        assert image.attrib == {
            "href": "#local",
            "{urn:foreign}href": "#other",
            f"{{{XLINK}}}href": "https://example.invalid/example.png",
        }
        assert svg.findtext("s:foreignObject/h:p/m:math/m:mi", namespaces=NS) == "x"
        assert svg.find("s:foreignObject/f:math/f:node", NS) is not None
        assert svg.find("s:foreignObject/plain/svg", NS) is not None
        assert root.find("h:body/f:math/f:mi", NS) is not None
        assert root.findtext("h:body/m:math/m:mi", namespaces=NS) == "y"
        other = root.find("h:body/f:other", NS)
        assert other is not None and other.attrib == {"{urn:foreign}flag": "yes", "flag": "no"}
        content = raw.decode()


def test_opf_properties_are_not_inferred_from_foreign_local_names(tmp_path):
    out = tmp_path / "foreign.epub"
    _write(
        out,
        f'<html xmlns="{XHTML}" xmlns:m="urn:foreign"><body>'
        f'<svg xmlns="{XHTML}"/>'
        '<m:math/><math xmlns="urn:foreign"/><m:svg/><m:script src="//example.invalid/a.js"/>'
        '<p m:onload="test"/><img m:href="//example.invalid/image.png"/>'
        "</body></html>",
    )
    raw, properties = _read(out)
    assert properties == set()
    assert ET.fromstring(raw).find("h:body/h:svg", NS) is not None


def test_ordinary_html_fallback_keeps_legacy_integration_and_entities(tmp_path):
    out = tmp_path / "html.epub"
    _write(
        out,
        "<!doctype html><HTML><BODY><p>Copyright &copy;<br>Next</p>"
        "<svg><foreignObject><p>HTML <math><mi>x</mi></math></p></foreignObject></svg>"
        "</BODY></HTML>",
    )
    raw, properties = _read(out)
    root = ET.fromstring(raw)
    assert properties == {"svg", "mathml"}
    paragraph = root.find("h:body/h:p", NS)
    assert paragraph is not None and paragraph.text == "Copyright ©" and paragraph[0].tail == "Next"
    assert root.findtext(".//s:foreignObject/h:p/m:math/m:mi", namespaces=NS) == "x"


def test_well_formed_html_keeps_legacy_foreign_content_integration(tmp_path):
    out = tmp_path / "html.epub"
    _write(
        out,
        f'<html xmlns="{XHTML}"><body><svg><foreignObject>'
        "<p>Intro <math><mi>x</mi></math></p>"
        '<section xmlns="urn:foreign"><math/></section>'
        "</foreignObject><rect/></svg></body></html>",
    )
    raw, properties = _read(out)
    root = ET.fromstring(raw)
    assert properties == {"svg", "mathml"}
    assert root.findtext(".//s:foreignObject/h:p/m:math/m:mi", namespaces=NS) == "x"
    assert root.find(".//s:foreignObject/f:section/f:math", NS) is not None
    assert root.find(".//s:svg/s:rect", NS) is not None


def test_entity_marker_is_literal_in_cdata_and_comments(tmp_path):
    out = tmp_path / "literal.epub"
    _write(
        out,
        '<!DOCTYPE html [<!-- <!ENTITY example "not a declaration"> -->]>'
        "<html><body><!-- <!ENTITY marker -->"
        '<p><![CDATA[<!ENTITY literal "text">]]></p></body></html>',
    )
    raw, properties = _read(out)
    assert b"<!-- <!ENTITY marker -->" in raw
    assert ET.fromstring(raw).findtext("h:body/h:p", namespaces=NS) == '<!ENTITY literal "text">'
    assert not properties


@pytest.mark.parametrize("reference", ["", "&example;"])
def test_real_entity_declarations_are_rejected(tmp_path, reference):
    out = tmp_path / "entity.epub"
    with pytest.raises(ValueError, match="unresolved XML entities"):
        _write(
            out,
            f'<!DOCTYPE html [<!ENTITY example "synthetic">]>'
            f"<html><body><p>{reference}</p></body></html>",
        )
    assert not out.exists()


def test_html_export_then_epub_preserves_prefixed_svg_and_mathml(tmp_path):
    source = tmp_path / "source.html"
    source.write_text(
        f'<!DOCTYPE html><html xmlns:m="{MATH}" xmlns:s="{SVG}"><head><title>Book</title></head>'
        "<body><p>Some text.</p><m:math><m:mi>x</m:mi></m:math>"
        '<s:svg><s:circle r="2"/></s:svg></body></html>',
        encoding="utf-8",
    )

    def store_for(path, name):
        store = FileStorage(str(tmp_path / name))
        store.init_from_document(load_document(str(path), "en", "zh"))
        return store

    store = store_for(source, "source-state")
    html = tmp_path / "exported.html"
    assemble(store, str(source), out_path=str(html), out_format="html", about_page=False)
    for origin, state in [(source, store), (html, store_for(html, "html-state"))]:
        epub = origin.with_suffix(".epub")
        assemble(state, str(origin), out_path=str(epub), out_format="epub", about_page=False)
        raw, properties = _read(epub)
        root = ET.fromstring(raw)
        assert properties == {"mathml", "svg"}
        assert root.findtext(".//m:math/m:mi", namespaces=NS) == "x"
        assert root.find(".//s:svg/s:circle[@r='2']", NS) is not None
        assert root.findtext(".//h:p", namespaces=NS) == "Some text."
