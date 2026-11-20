"""Offline WordprocessingML compatibility tests using hand-built and Word-like fixtures."""

from __future__ import annotations

from contextlib import closing
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.docx_reader import read_docx
from wenyi_core.markup import _epub_native
from wenyi_core.storage.file import FileStorage

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W}


def _child(parent, tag, *, value=None, **attrs):
    node = OxmlElement(f"w:{tag}")
    if value is not None:
        attrs["val"] = value
    for key, val in attrs.items():
        node.set(qn(f"w:{key}"), str(val))
    parent.append(node)
    return node


def _store(tmp_path, book):
    store = FileStorage(str(tmp_path / "state"))
    store.save_manifest(store.stage_document(book))
    return store


def test_docx_failed_write_preserves_previous_export(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"
    output = tmp_path / "existing.docx"
    original = Document()
    original.add_paragraph("A document.")
    original.save(source)
    output.write_bytes(b"previous export")

    def fail(path, _header, _chapters):
        path.write_bytes(b"incomplete DOCX")
        raise OSError("simulated native write failure")

    monkeypatch.setattr(_epub_native, "write_docx", fail)
    with closing(_store(tmp_path, read_docx(str(source), "en", "zh"))) as store:
        with pytest.raises(OSError, match="simulated native write failure"):
            assemble(store, str(source), str(output), "docx")
    assert output.read_bytes() == b"previous export"
    assert not list(tmp_path.glob(".wenyi-*.tmp"))


def test_strict_docx_reads_related_utf16_parts_and_cached_toc(tmp_path):
    source = tmp_path / "source.docx"
    office = "http://purl.oclc.org/ooxml/officeDocument/relationships"
    with ZipFile(source, "w") as zip:
        zip.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{REL}"><Relationship Id="office" '
            f'Type="{office}/officeDocument" Target="/parts/main%20doc.xml"/></Relationships>',
        )
        zip.writestr(
            "parts/_rels/main doc.xml.rels",
            f'<Relationships xmlns="{REL}"><Relationship Id="styles" '
            f'Type="{office}/styles" Target="../styles/word.xml"/></Relationships>',
        )
        zip.writestr(
            "styles/word.xml",
            f'<x:styles xmlns:x="{STRICT}"><x:style x:type="paragraph" x:styleId="custom">'
            '<x:name x:val="Custom chapter"/><x:pPr><x:outlineLvl x:val="0"/></x:pPr>'
            "</x:style></x:styles>",
        )
        zip.writestr(
            "parts/main doc.xml",
            (
                f'<?xml version="1.0" encoding="UTF-16"?><x:document xmlns:x="{STRICT}"><x:body>'
                "<x:sdt><x:sdtContent><x:p><x:pPr><x:pStyle x:val='TOC2'/></x:pPr>"
                "<x:r><x:instrText>TOC</x:instrText></x:r><x:hyperlink>"
                "<x:r><x:t>1. Élan</x:t><x:tab/><x:t>2</x:t></x:r></x:hyperlink>"
                "</x:p></x:sdtContent></x:sdt>"
                '<x:p><x:pPr><x:pStyle x:val="custom"/></x:pPr>'
                "<x:r><x:t>Élan</x:t></x:r></x:p></x:body></x:document>"
            ).encode("utf-16"),
        )
    book = read_docx(str(source), "fr", "en")
    assert [ch.title for ch in book.chapters] == ["source", "Élan"]
    entry = book.chapters[0].segments[0]
    assert (entry.source, entry.kind, entry.meta["docx_toc_level"]) == ("1. Élan\t2", "text", 2)
    assert book.chapters[1].segments[0].meta["heading_level"] == 1
    entry.target = "1. Momentum\t2"
    store = _store(tmp_path, book)
    output = tmp_path / "out.docx"
    assemble(store, str(source), str(output), "docx")
    result = Document(output)
    assert result.paragraphs[0].style.style_id == "TOC2"
    assert result.paragraphs[0].text == "1. Momentum\t2"
    assert result.paragraphs[1].style.name == "Heading 1"
    assert result.paragraphs[0]._p.find(".//" + qn("w:numPr")) is None


@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_docx_list_instances_keep_nested_formats_and_restart_from_saved_state(tmp_path, order):
    source = tmp_path / "source.docx"
    original = Document()
    numbering = original.part.numbering_part.element
    definition = _child(numbering, "abstractNum", abstractNumId=99)
    # Word places all abstract numbering definitions before the instances.
    numbering.insert(0, definition)
    for index, fmt, marker in [(0, "upperRoman", "%1."), (1, "lowerLetter", "%1.%2)")]:
        level = _child(definition, "lvl", ilvl=index)
        _child(level, "start", value=1)
        _child(level, "numFmt", value=fmt)
        if index == 1:
            _child(level, "lvlRestart", value=0)
        _child(level, "lvlText", value=marker)
    for num_id, start in [(80, 3), (81, 8)]:
        instance = _child(numbering, "num", numId=num_id)
        _child(instance, "abstractNumId", value=99)
        _child(_child(instance, "lvlOverride", ilvl=0), "startOverride", value=start)
    for text, num_id, ilvl in [
        ("Parent", 80, 0),
        ("Child", 80, 1),
        ("1. Already numbered", 80, 0),
        ("Next parent", 80, 0),
        ("Restart", 81, 0),
    ]:
        p = original.add_paragraph(text)
        np = p._p.get_or_add_pPr().get_or_add_numPr()
        np.get_or_add_numId().val = num_id
        np.get_or_add_ilvl().val = ilvl
    original.save(source)

    book = read_docx(str(source), "en", "zh")
    segments = book.chapters[0].segments
    assert [s.meta.get("list_fmt") for s in segments] == [
        "upperRoman",
        "lowerLetter",
        None,
        "upperRoman",
        "upperRoman",
    ]
    for seg in segments:
        seg.target = f"译：{seg.source}"
    store = _store(tmp_path, book)
    assert store.load_manifest()["meta"]["docx_numbering"]["81"]["0"]["start_override"] == 8
    source.unlink()  # Export should use the stored definitions, not reopen the DOCX.
    output = tmp_path / "out.docx"
    assemble(store, str(source), str(output), "docx", bilingual=True, order=order)
    with ZipFile(output) as zip:
        doc = ET.fromstring(zip.read("word/document.xml"))
        lists = ET.fromstring(zip.read("word/numbering.xml"))

    def value(node, name):
        return node.find(name, NS).get(f"{{{W}}}val")

    seen = []
    for paragraph in doc.findall("w:body/w:p", NS):
        label = paragraph.find("w:r/w:t", NS).text
        num = paragraph.find("w:pPr/w:numPr", NS)
        if num is not None:
            assert label.startswith("译：")
            seen.append((value(num, "w:numId"), value(num, "w:ilvl")))
    assert [level for _, level in seen] == ["0", "1", "0", "0"]
    first, child, next_parent, restarted = [num_id for num_id, _ in seen]
    assert first == child == next_parent != restarted
    for num_id, expected in [(first, "3"), (restarted, "8")]:
        instance = lists.find(f"w:num[@w:numId='{num_id}']", NS)
        assert value(instance, "w:lvlOverride/w:startOverride") == expected
        abstract_id = value(instance, "w:abstractNumId")
        abstract = lists.find(f"w:abstractNum[@w:abstractNumId='{abstract_id}']", NS)
        nested = abstract.find("w:lvl[@w:ilvl='1']", NS)
        assert value(nested, "w:numFmt") == "lowerLetter"
        assert value(nested, "w:lvlText") == "%1.%2)"
        assert value(nested, "w:lvlRestart") == "0"


def test_docx_keeps_order_merge_geometry_and_source_fonts_on_export(tmp_path):
    source = tmp_path / "source.docx"
    original = Document()
    original.core_properties.title = "A & B < C"
    original.core_properties.author = "Élan"
    style = original.styles.add_style("Centered", WD_STYLE_TYPE.PARAGRAPH)
    style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    style.paragraph_format.space_after = Pt(9)
    style.font.name = "Source & Serif"
    paragraph = original.add_paragraph(style=style)
    paragraph.add_run("  🌳 ")
    paragraph.add_run("bold").bold = True
    paragraph.add_run(" end  ")
    table = original.add_table(rows=2, cols=3)
    table.cell(0, 0).merge(table.cell(1, 1)).text = "Wide & tall"
    table.cell(0, 2).text = "Upper"
    table.cell(1, 2).text = "Lower"
    original.add_paragraph("After")
    original.save(source)

    book = read_docx(str(source), "en", "zh")
    chapter = book.chapters[0]
    before, wide, *rest = chapter.segments
    assert before.source == "🌳 bold end"
    assert before.meta["docx_styles"]["items"] == [
        {"id": "s1", "mode": "range", "source_start": 2, "source_end": 6, "bold": True}
    ]
    assert before.meta["docx_source_runs"][0]["fonts"]["ascii"] == "Source & Serif"
    assert (wide.meta["docx_grid_span"], wide.meta["docx_row_span"]) == (2, 2)
    assert [s.source for s in rest] == ["", "Upper", "", "", "Lower", "After"]
    before.target = "树 粗体 尾"
    wide.source = "Wide & "
    wide.target = "又宽"
    chapter.segments.insert(
        2, wide.model_copy(update={"source": "tall", "target": "又高", "meta": {}, "cont": True})
    )
    for index, segment in enumerate(chapter.segments):
        segment.index = index

    store = _store(tmp_path, book)
    assert store.load_manifest()["meta"]["docx_core_properties"]["creator"] == "Élan"
    output = tmp_path / "out.docx"
    assemble(store, str(source), str(output), "docx", bilingual=True, preserve_source_style=True)
    result = Document(output)
    assert result.core_properties.title == "A & B < C"
    assert result.core_properties.author == "Élan"
    assert [node.tag for node in result.element.body] == [
        qn("w:p"),
        qn("w:p"),
        qn("w:tbl"),
        qn("w:p"),
        qn("w:sectPr"),
    ]
    target, fallback = result.paragraphs[:2]
    assert target.alignment == fallback.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert fallback.paragraph_format.space_after == Pt(9)
    assert {r.font.name for r in target.runs} == {"宋体"}
    assert {r.font.name for r in fallback.runs} == {"Source & Serif"}
    assert next(r.text for r in fallback.runs if r.bold) == "bold"
    wide = result.tables[0].cell(0, 0)
    assert wide.text == "又宽又高\nWide & tall"
    assert result.tables[0].cell(1, 1)._tc is wide._tc
    assert result.tables[0].cell(1, 2).text == "Lower"


def test_docx_numbered_cell_numbers_the_target_even_when_source_is_first(tmp_path):
    source = tmp_path / "source.docx"
    original = Document()
    paragraph = original.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
    paragraph.style = "List Number"
    paragraph.add_run("Item")
    original.save(source)

    book = read_docx(str(source), "en", "zh")
    book.chapters[0].segments[0].target = "项目"
    store = _store(tmp_path, book)
    output = tmp_path / "out.docx"
    assemble(store, str(source), str(output), "docx", bilingual=True, order="source_first")
    paragraphs = Document(output).tables[0].cell(0, 0).paragraphs
    assert [p.text for p in paragraphs] == ["Item", "项目"]
    assert [p._p.find(".//" + qn("w:numPr")) is not None for p in paragraphs] == [False, True]


def test_docx_rejects_non_document_input(tmp_path):
    source = tmp_path / "source.docx"
    source.write_text("not a ZIP")
    with pytest.raises(ValueError, match="Cannot read Word document: Invalid Word ZIP archive"):
        read_docx(str(source), "en", "zh")
