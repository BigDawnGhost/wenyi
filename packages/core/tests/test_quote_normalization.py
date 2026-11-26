"""Pure-function and multi-format export tests for output.quote_style normalization."""

from __future__ import annotations

import zipfile

import pytest
from docx import Document as DocxDocument
from wenyi_core.assemble.quotes import normalize_quotes
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.file import FileStorage


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Nested quotes
        ("\"She said, 'Hello!'\"", "“She said, ‘Hello!’”"),
        ("\"'Hello,' he said.\"", "“‘Hello,’ he said.”"),
        # Contractions & possessives
        (
            "I don't think it's James' hat or the dogs' bowls.",
            "I don’t think it’s James’ hat or the dogs’ bowls.",
        ),
        ("Rock'n'roll at o'clock.", "Rock’n’roll at o’clock."),
        # Leading elisions & decades
        (
            "Tell 'em about the '90s, 'tis true, 'cause I said so.",
            "Tell ’em about the ’90s, ’tis true, ’cause I said so.",
        ),
        ('"\'Tis true," he muttered.', "“’Tis true,” he muttered."),
        # Em-dashes & ellipses
        ('"Wait—" he interrupted.', "“Wait—” he interrupted."),
        ('He paused—"Really?"', "He paused—“Really?”"),
        ('"Well..." she sighed.', "“Well...” she sighed."),
        ('"...and then it stopped."', "“...and then it stopped.”"),
        ("\"Wait—'no'—stop!\"", "“Wait—‘no’—stop!”"),
        # Markup & protected-object boundaries
        ('"<em>hello</em>"', "“<em>hello</em>”"),
        ('<a href="https://example.com">"link"</a>', '<a href="https://example.com">“link”</a>'),
        ('〘object:eq1〙"quoted"', "〘object:eq1〙“quoted”"),
        # Mixed curly/straight unified cleanly
        ("“If You Don't Deserve to Die\"", "“If You Don’t Deserve to Die”"),
    ],
)
def test_normalize_quotes_typographic_boundaries(raw, expected):
    assert normalize_quotes(raw, "typographic", target_lang="en") == expected
    # Idempotent when applied twice.
    assert normalize_quotes(expected, "typographic", target_lang="en") == expected


def test_normalize_quotes_straight_preserve_and_non_latin():
    mixed = '“She said, ‘Don’t tell ’em about the ’90s.’” <a href="x">“ok”</a>'
    assert (
        normalize_quotes(mixed, "straight", target_lang="en")
        == '"She said, \'Don\'t tell \'em about the \'90s.\'" <a href="x">"ok"</a>'
    )
    assert normalize_quotes(mixed, "preserve", target_lang="en") == mixed
    # Non-Latin targets are never rewritten.
    zh = '"他说"don\'t"'
    assert normalize_quotes(zh, "typographic", target_lang="zh") == zh
    assert normalize_quotes(zh, "straight", target_lang="ja") == zh


@pytest.mark.parametrize(
    ("text", "target_lang"),
    [
        ("\"Hallo,\" sagte sie. 'Na ja.'", "de"),
        ("„Hallo“, sagte sie.", "de-AT"),
        ('"Bonjour," dit-il.', "fr"),
        ("« Bonjour », dit-il. L'homme partit.", "fr"),
        ('"Hola," dijo.', "es"),
        ("\"Ciao,\" disse. 'Sì.'", "it"),
        ('"Olá," disse.', "pt-BR"),
        ('"Xin chào," cô nói.', "vi"),
    ],
)
def test_typographic_quotes_leave_non_english_targets_unchanged(text, target_lang):
    # English curly quotes and elision rules are wrong for these languages' conventions.
    assert normalize_quotes(text, "typographic", target_lang=target_lang) == text


def test_typographic_quotes_apply_to_english_regional_targets():
    assert normalize_quotes('"Hi," he said.', "typographic", target_lang="en-GB") == (
        "“Hi,” he said."
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("„Hallo“, sagte sie. ‚Na ja‘.", "\"Hallo\", sagte sie. 'Na ja'."),
        ("“Bonjour,” dit-il. L’homme partit.", '"Bonjour," dit-il. L\'homme partit.'),
        ("« Bonjour », dit-il.", "« Bonjour », dit-il."),
    ],
)
def test_straight_quotes_convert_every_curly_quote_without_mixing_styles(text, expected):
    lang = "fr" if "Bonjour" in text else "de"
    assert normalize_quotes(text, "straight", target_lang=lang) == expected


def test_output_quote_style_config_validation():
    assert Config().output.quote_style == "preserve"
    assert (
        Config.from_dict({"output": {"quote_style": "typographic"}}).output.quote_style
        == "typographic"
    )
    assert (
        Config.from_dict({"output": {"quote_style": "straight"}}).output.quote_style == "straight"
    )
    with pytest.raises(ValueError):
        Config.from_dict({"output": {"quote_style": "invalid"}})


@pytest.mark.parametrize("bilingual", [False, True], ids=["mono", "bilingual"])
@pytest.mark.parametrize("quote_style", ["typographic", "straight"])
def test_quote_style_applies_at_export_only_and_keeps_bilingual_source_unchanged(
    tmp_path, bilingual, quote_style
):
    src_file = tmp_path / "book.txt"
    src_file.write_text("# 标题\n\n原文\n", encoding="utf-8")
    source_line = "原文“引号”与\"直引号\"和'单引号'"
    raw_target = "\"Don't tell 'em,\" she said. “It's James' '90s car.”"
    expected_target = normalize_quotes(raw_target, quote_style, target_lang="en")
    raw_title = 'If You Don\'t "Deserve" to Die'
    expected_title = normalize_quotes(raw_title, quote_style, target_lang="en")

    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(
        Document(
            title="原书",
            source_lang="zh",
            target_lang="en",
            fmt="txt",
            source_path=str(src_file),
            chapters=[
                Chapter(
                    index=0,
                    title="第一章",
                    segments=[Segment(index=0, source=source_line, target=raw_target)],
                )
            ],
        )
    )
    manifest = store.load_manifest()
    manifest["chapters"][0]["title_translated"] = raw_title
    store.save_manifest(manifest)

    # EPUB
    epub_out = tmp_path / f"out_{quote_style}_{bilingual}.epub"
    assemble(
        store,
        str(src_file),
        out_path=str(epub_out),
        out_format="epub",
        bilingual=bilingual,
        quote_style=quote_style,
    )
    with zipfile.ZipFile(epub_out) as zf:
        ch_name = next(n for n in zf.namelist() if n.endswith("ch0.xhtml"))
        ch_html = zf.read(ch_name).decode("utf-8")
    assert expected_target in ch_html
    assert expected_title in ch_html
    if bilingual:
        assert source_line in ch_html

    # HTML
    html_out = tmp_path / f"out_{quote_style}_{bilingual}.html"
    assemble(
        store,
        str(src_file),
        out_path=str(html_out),
        out_format="html",
        bilingual=bilingual,
        quote_style=quote_style,
    )
    html_text = html_out.read_text(encoding="utf-8")
    assert expected_target in html_text
    if bilingual:
        assert source_line in html_text

    # DOCX
    docx_out = tmp_path / f"out_{quote_style}_{bilingual}.docx"
    assemble(
        store,
        str(src_file),
        out_path=str(docx_out),
        out_format="docx",
        bilingual=bilingual,
        quote_style=quote_style,
    )
    docx_text = "\n".join(p.text for p in DocxDocument(str(docx_out)).paragraphs)
    assert expected_target in docx_text
    if bilingual:
        assert source_line in docx_text

    # Saved state is completely untouched!
    assert store.load_chapter(0).segments[0].target == raw_target
    assert store.load_manifest()["chapters"][0]["title_translated"] == raw_title


def test_normalize_quotes_preserves_gt_inside_quoted_html_attributes():
    html = '<a title="x > y">"Hello," she said.</a>'
    assert normalize_quotes(html, "typographic", target_lang="en") == (
        '<a title="x > y">“Hello,” she said.</a>'
    )


def test_normalize_quotes_respects_block_boundaries_and_explicit_single_openers():
    assert normalize_quotes('<p>"Hello"</p><p>"World"</p>', "typographic", target_lang="en") == (
        "<p>“Hello”</p><p>“World”</p>"
    )
    assert normalize_quotes("‘Round the world,’ she read.", "typographic", target_lang="en") == (
        "‘Round the world,’ she read."
    )
    assert normalize_quotes("'round the corner", "typographic", target_lang="en") == (
        "’round the corner"
    )


def test_export_view_normalizes_quotes_across_continuation_segments(tmp_path):
    from wenyi_core.assemble.export_view import ExportViewStore

    src_file = tmp_path / "split.txt"
    src_file.write_text("甲乙\n", encoding="utf-8")
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(
        Document(
            title="Split",
            source_lang="zh",
            target_lang="en",
            fmt="txt",
            source_path=str(src_file),
            chapters=[
                Chapter(
                    index=0,
                    title="Ch 1",
                    segments=[
                        Segment(index=0, source="甲", target='"First half ', cont=False),
                        Segment(index=1, source="乙", target='second half."', cont=True),
                    ],
                )
            ],
        )
    )
    view = ExportViewStore(store, quote_style="typographic")
    chapter = view.load_chapter(0)
    assert chapter.segments[0].target == "“First half "
    assert chapter.segments[1].target == "second half.”"


def test_preserve_style_never_reports_normalization():
    from wenyi_core.assemble.quotes import supports_quote_normalization

    assert supports_quote_normalization("en", "preserve") is False
