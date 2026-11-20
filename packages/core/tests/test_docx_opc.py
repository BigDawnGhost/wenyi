"""End-to-end tests for resolving DOCX relationships within the package."""

from xml.sax.saxutils import quoteattr
from zipfile import ZipFile

import pytest
from docx import Document
from wenyi_core.ingest.docx_reader import read_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def _relationships(kind, target, mode=""):
    return (
        f'<Relationships xmlns="{REL}"><Relationship Id="rId1" '
        f'Type="{OFFICE}/{kind}" Target={quoteattr(target)}{mode}/></Relationships>'
    )


def _package(path, main_target, main_name, styles=None, *, extra=None, mode=""):
    types = [
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        f'<Override PartName={quoteattr("/" + main_name)} ContentType="{WORD_TYPE}document.main+xml"/>',
    ]
    with ZipFile(path, "w") as archive:
        archive.writestr("_rels/.rels", _relationships("officeDocument", main_target, mode))
        archive.writestr(
            main_name,
            f'<w:document xmlns:w="{W}"><w:body><w:p><w:pPr><w:pStyle w:val="Custom"/>'
            "</w:pPr><w:r><w:t>Chapter text</w:t></w:r></w:p></w:body></w:document>",
        )
        if styles is not None:
            target, name = styles
            directory, _, leaf = main_name.rpartition("/")
            prefix = f"{directory}/" if directory else ""
            archive.writestr(f"{prefix}_rels/{leaf}.rels", _relationships("styles", target))
            archive.writestr(
                name,
                f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:styleId="Custom">'
                '<w:name w:val="Heading 1"/></w:style></w:styles>',
            )
            types.append(
                f'<Override PartName={quoteattr("/" + name)} ContentType="{WORD_TYPE}styles+xml"/>'
            )
        archive.writestr("[Content_Types].xml", f'<Types xmlns="{TYPES}">{"".join(types)}</Types>')
        for name, body in (extra or {}).items():
            archive.writestr(name, body)


@pytest.mark.parametrize(
    ("main_target", "main_name", "styles"),
    [
        pytest.param(
            "word/document.xml",
            "word/document.xml",
            ("styles.xml", "word/styles.xml"),
            id="ordinary",
        ),
        pytest.param(
            "word/main%20text.xml",
            "word/main%20text.xml",
            ("/styles/%C3%89lan%2520.xml", "styles/%C3%89lan%2520.xml"),
            id="encoded-root-and-absolute",
        ),
        pytest.param(
            "/word/nested%20dir/deeper/main%20text.xml",
            "word/nested%20dir/deeper/main%20text.xml",
            (".././styles%20text.xml", "word/nested%20dir/styles%20text.xml"),
            id="encoded-nested-relative",
        ),
        pytest.param(
            "/word/nested/document.xml",
            "word/nested/document.xml",
            ("../styles%20text.xml", "word/styles%20text.xml"),
            id="encoded-subordinate-only",
        ),
    ],
)
def test_docx_resolves_opc_part_uris(tmp_path, main_target, main_name, styles):
    source = tmp_path / "source.docx"
    _package(source, main_target, main_name, styles)

    assert Document(source).paragraphs[0].style.name == "Heading 1"
    _assert_heading(source)


@pytest.mark.parametrize("escape", ["%23", "%3F", "%3A"])
def test_docx_resolves_escaped_reserved_uri_data(tmp_path, escape):
    source = tmp_path / "source.docx"
    main = f"word/nested/chapter{escape}notes.xml"
    _package(
        source,
        main,
        main,
        (f"../styles{escape}notes.xml", f"word/styles{escape}notes.xml"),
    )

    assert Document(source).paragraphs[0].style.name == "Heading 1"
    _assert_heading(source)


def _assert_heading(source):
    (chapter,) = read_docx(str(source), "en", "zh").chapters
    (segment,) = chapter.segments
    assert (chapter.title, segment.source, segment.meta["heading_level"]) == (
        "Chapter text",
        "Chapter text",
        1,
    )


def test_docx_accepts_legacy_decoded_zip_names(tmp_path):
    source = tmp_path / "source.docx"
    _package(
        source,
        "/parts%20dir/main%20text.xml",
        "parts dir/main text.xml",
        ("styles%20%C3%89lan.xml", "parts dir/styles Élan.xml"),
    )
    _assert_heading(source)


def test_docx_prefers_exact_parts_to_legacy_zip_aliases(tmp_path):
    source = tmp_path / "source.docx"
    _package(
        source,
        "word/main%20text.xml",
        "word/main%20text.xml",
        ("styles%20text.xml", "word/styles%20text.xml"),
        extra={
            "word/main text.xml": "not XML",
            "word/_rels/main text.xml.rels": "not XML",
            "word/styles text.xml": "not XML",
        },
    )
    _assert_heading(source)


@pytest.mark.parametrize(
    ("escaped", "decoded"), [("%2520", "%20"), ("%23", "#"), ("%3F", "?"), ("%3A", ":")]
)
def test_docx_does_not_unescape_reserved_data_for_legacy_lookup(tmp_path, escaped, decoded):
    source = tmp_path / "source.docx"
    _package(source, f"word/main{escaped}text.xml", f"word/main{decoded}text.xml")
    with pytest.raises(ValueError, match=f"Missing DOCX part word/main{escaped}text.xml"):
        read_docx(str(source), "en", "zh")


@pytest.mark.parametrize(
    "target",
    [
        "../word/document.xml",
        "https://example.invalid/document.xml",
        "file:///word/document.xml",
        "file:document.xml",
        "//word/document.xml",
        r"word\document.xml",
        "word/document.xml?query",
        "word/document.xml#fragment",
        "word%2Fdocument.xml",
        "word%5Cdocument.xml",
        "word/%2e%2e/word/document.xml",
        "word/document.xml%00",
        "word/document%ZZ.xml",
        "word/document%FF.xml",
    ],
)
def test_docx_rejects_invalid_internal_opc_targets(tmp_path, target):
    source = tmp_path / "source.docx"
    _package(source, target, "word/document.xml")
    with pytest.raises(ValueError, match="Invalid DOCX relationship target"):
        read_docx(str(source), "en", "zh")


def test_docx_rejects_subordinate_target_above_package_root(tmp_path):
    source = tmp_path / "source.docx"
    main = "word/nested/document.xml"
    _package(source, main, main, ("../../../styles.xml", "styles.xml"))
    with pytest.raises(ValueError, match="Invalid DOCX relationship target"):
        read_docx(str(source), "en", "zh")


def test_docx_ignores_explicit_external_targets(tmp_path):
    source = tmp_path / "source.docx"
    _package(source, "word/document.xml", "word/document.xml", mode=' TargetMode="External"')
    with pytest.raises(ValueError, match="No main Word document part"):
        read_docx(str(source), "en", "zh")
