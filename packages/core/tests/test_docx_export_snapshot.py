"""Golden comparison against the original Python DOCX planner on saved state."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from contextlib import closing
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from wenyi_core.assemble.docx_writer import _assemble_docx
from wenyi_core.assemble.writer import assemble
from wenyi_core.document_styles.docx import proportional_range_placements
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.file import FileStorage


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _saved_document() -> Document:
    # Use saved metadata directly, without relying on the current DOCX ingester
    # to recreate fields that older projects did not have.
    def chapter(index, title, meta, *segments):
        return Chapter(
            index=index,
            title=title,
            meta=meta,
            segments=[Segment(index=i, **s) for i, s in enumerate(segments)],
        )

    def segment(source, target=None, *, kind="text", cont=False, **meta):
        return dict(source=source, target=target, kind=kind, cont=cont, meta=meta)

    def cell(source, target, col, *, row=0, table_id=4, **meta):
        return segment(source, target, table_id=table_id, row=row, col=col, **meta)

    return Document(
        title="Saved DOCX",
        source_lang="ja",
        target_lang="zh",
        fmt="docx",
        chapters=[
            chapter(
                0,
                "Stored title",
                {"explicit_title": True, "heading_level": 3},
                segment("章", "一节", kind="heading"),  # Chapter-only heading level.
                segment(
                    "a🌳b",
                    "甲树,",
                    align="center",
                    shade="ABA0F0",
                    docx_source_runs=[
                        {"source_start": 0, "source_end": 6, "fonts": {"ascii": "Saved Serif"}}
                    ],
                    docx_paragraph={"spacing": {"after": "120"}},
                    docx_tabs=[{"val": "right", "pos": "400"}],
                    docx_styles={
                        "target_digest": _digest("甲树,乙丙丁"),
                        "items": [{"id": "s1", "source_start": 0, "source_end": 2, "bold": True}],
                        "placements": [
                            {"target_start": 1, "target_end": 4, "italic": True},
                            {"target_start": 2, "target_end": 5, "color": "A01010"},
                            {"target_start": "5", "target_end": 6, "underline": True},
                        ],
                    },
                ),
                segment("cde", "乙丙丁", cont=True, docx_style={"underline": True}),
                segment(
                    "x😀🌳y",
                    "戊己庚",
                    docx_styles={
                        "target_digest": _digest("older target"),
                        "items": [
                            {"id": "s2", "source_start": 1, "source_end": 2, "underline": True},
                            {"id": "s3", "source_start": -1, "source_end": 1, "bold": True},
                        ],
                        "placements": [{"target_start": 0, "target_end": 3, "italic": True}],
                    },
                ),
                segment(
                    "legacy",
                    "辛壬癸",
                    docx_styles={
                        "items": [{"id": "s4", "source_start": 2, "source_end": 6, "size_pt": 20}],
                    },
                ),
                segment(
                    "読み〘よみ〙",
                    "读音",
                    docx_style={"bold": True, "font": "Old Font"},
                    docx_styles={"items": [{"id": "unused"}]},
                ),
                segment(
                    "1. TOC\t3", "1. 目录\t3", docx_toc_level=2, list_num_id=7, list_fmt="decimal"
                ),
                segment("Empty target", "\x1c", docx_style={"italic": True}),
                segment("one ", "单"),
                segment("side", None, cont=True, table_id=None),
                segment("Ignored attr", "不导出", html_attribute={}),
                segment("Ignored continuation", cont=True),
                cell("Wide ", "很", 0, rows=2, cols=3, docx_grid_span=2, docx_row_span=2),
                segment("cell", "宽", cont=True),
                cell("Hidden attr inside table", "不导出", 2, html_attribute=None),
                cell("Item", "表项", 2, list_num_id=7, list_ilvl=10, list_fmt="decimal"),
                cell("Skipped merge", "不导出", 0, row=1, docx_merge_continuation=True),
                cell("Old cell", "旧单元格", 2, row=1, align="right"),
                cell("Next table", "下表", 0, table_id=5, cont=True),
                segment("After"),
                cell("Same id but new table", "另一张表", 0, table_id=5),
            ),
            chapter(
                1,
                "Suppress this title",
                {"explicit_title": True, "heading_level": 4},
                segment("Heading one", "一级标题", kind="heading", heading_level=None),
                segment("Deep heading", "九级标题", kind="heading", heading_level=17),
                segment("Old list", "旧列表", list_num_id=9, list_fmt="bullet"),
            ),
        ],
        meta={
            "docx_numbering": {
                "7": {"8": {"spec": {"numFmt": "decimal", "lvlText": "%9)"}, "start_override": 3}}
            },
            "docx_core_properties": {"title": "Saved & stored", "creator": "森"},
        },
    )


# Hashes captured with the original Python planner and native renderer. Compare
# the uncompressed XML, not the ZIP metadata.
_GOLDEN = [
    (
        "target_first",
        False,
        False,
        "8ae050a1d64b1f3de25a5cf89f3c89453455ca4c9369a32b8625aa1cd9398597",
    ),
    (
        "target_first",
        True,
        False,
        "40049681d5f62e1193d5f1de26268b06a9bbb22ea81476619de955cf3dcaaf6d",
    ),
    (
        "source_first",
        True,
        True,
        "974b1be05ca8101d94f28b208225aaf34cb4aff9588a43c70ef362d1805dd293",
    ),
]


@pytest.mark.parametrize("order,bilingual,preserve,expected", _GOLDEN)
def test_saved_docx_xml_matches_python_planner(tmp_path, order, bilingual, preserve, expected):
    missing_source = tmp_path / "not-present.docx"
    output = tmp_path / "result.docx"
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        manifest = store.stage_document(_saved_document(), source_hash="a" * 64)
        manifest["chapters"][0]["title_translated"] = "  已保存的标题  "
        store.save_manifest(manifest)
        snapshot = store.create_export_snapshot(actual_sha256="a" * 64)
        # Export must still use the frozen text and not consult the live state.
        chapter = store.load_chapter(0)
        chapter.segments[1].target = "live edit"
        store.save_chapter(chapter)
        assemble(
            snapshot,
            str(missing_source),
            str(output),
            "docx",
            order=order,
            bilingual=bilingual,
            preserve_source_style=preserve,
        )
        assert snapshot.load_chapter(0).segments[1].target == "甲树,"
    with ZipFile(output) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        assert "甲树,乙丙丁" in ["".join(p.itertext()) for p in document.findall(".//{*}p")]
        for name, digest in [
            ("word/document.xml", expected),
            (
                "word/numbering.xml",
                "1a33b34f10a7229ca17eaf70299ef4eb80d933e495cd55ba139bf369c1612e15",
            ),
            (
                "docProps/core.xml",
                "842d120e94bb0ccf11b0acdd6c5c58ca5cca6d9755e7ec6b100b182a4ceeb4b2",
            ),
        ]:
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name


@pytest.mark.parametrize(
    "source,target,start,end",
    [
        ("", "甲😀乙", 0, 1),
        ("a🌳bc", "😀甲e\u0301乙", 2, 9),
        ("ab🌳c", "甲😀乙", 1, 2),
        ("ab🌳c", "甲😀乙", 2, 1),
        ("🌳abc", "甲😀乙", -2, 2),
    ],
)
def test_docx_fallback_run_offsets_match_python(tmp_path, source, target, start, end):
    item = {"id": "s1", "source_start": start, "source_end": end, "bold": True}
    [projected] = proportional_range_placements(source, target, [item])
    segment = Segment(
        index=0, source=source, target=target, meta={"docx_styles": {"items": [item]}}
    )
    document = Document(
        source_lang="en",
        target_lang="zh",
        fmt="docx",
        chapters=[Chapter(index=0, segments=[segment])],
    )
    output = tmp_path / "out.docx"
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.save_manifest(store.stage_document(document, source_hash="a" * 64))
        _assemble_docx(store, str(output))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(output) as archive:
        runs = ET.fromstring(archive.read("word/document.xml")).findall("w:body/w:p/w:r", ns)
    assert "".join(run.findtext("w:t", default="", namespaces=ns) for run in runs) == target
    assert (
        "".join(
            run.findtext("w:t", default="", namespaces=ns)
            for run in runs
            if run.find("w:rPr/w:b", ns) is not None
        )
        == target[max(0, projected["target_start"]) : max(0, projected["target_end"])]
    )


def test_docx_late_snapshot_read_error_is_not_published(tmp_path, monkeypatch):
    output = tmp_path / "existing.docx"
    output.write_bytes(b"previous export")
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.save_manifest(store.stage_document(_saved_document(), source_hash="a" * 64))
        snapshot = store.create_export_snapshot(actual_sha256="a" * 64)
        original = snapshot.load_chapter
        read = []

        def load_chapter(index):
            read.append(index)
            if index == 1:
                raise OSError("snapshot read failed")
            return original(index)

        monkeypatch.setattr(snapshot, "load_chapter", load_chapter)
        with pytest.raises(OSError, match="snapshot read failed"):
            _assemble_docx(snapshot, str(output))
    assert read == [0, 1]
    assert output.read_bytes() == b"previous export"
    assert not list(tmp_path.glob(".wenyi-*.tmp"))


def test_docx_export_does_not_import_python_docx(tmp_path):
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.save_manifest(store.stage_document(_saved_document(), source_hash="a" * 64))
    output = tmp_path / "out.docx"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from contextlib import closing

sys.modules["docx"] = None
from wenyi_core.assemble.writer import assemble
from wenyi_core.storage.file import FileStorage

with closing(FileStorage(sys.argv[1])) as store:
    snapshot = store.create_export_snapshot(actual_sha256="a" * 64)
    assemble(snapshot, "missing.docx", sys.argv[2], "docx")
""",
            str(tmp_path / "state"),
            str(output),
        ],
        check=True,
    )
    with ZipFile(output) as archive:
        assert b"Heading3" in archive.read("word/document.xml")
