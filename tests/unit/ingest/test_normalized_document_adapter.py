"""Ingest ``Document`` 到耐久化规范文档的适配边界测试。"""

from __future__ import annotations

import inspect
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from trans_novel.domain.normalized_document import (
    NORMALIZED_DOCUMENT_SCHEMA_VERSION,
    decode_normalized_document_v1,
)
from trans_novel.ingest.html_reader import read_html
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.ingest.normalized_document_adapter import (
    encode_ingest_document_v1,
    normalized_document_v1_from_ingest,
)

SOURCE_HASH = "a" * 64


def _annotation_meta() -> dict[str, object]:
    """构造 HTML/EPUB 读取器实际产生的源侧注释元数据。"""
    return {
        "version": 1,
        "source_length": 4,
        "items": [
            {
                "id": "tn0_0_annotation_0",
                "mode": "point",
                "source_start": 4,
                "source_end": 4,
                "source_text": "",
                "marker_text": "1",
                "raw_href": "#note-1",
                "target_key": "chapter.xhtml#note-1",
                "relation": "noteref",
            }
        ],
    }


def _inline_meta() -> dict[str, object]:
    """构造 HTML 标注器在模板回填前产生的源侧内联节点元数据。"""
    return {
        "version": 1,
        "source_length": 4,
        "nodes": [
            {
                "id": "tn0_0_inline_0",
                "tag": "img",
                "placement": "after",
                "offset": 4,
            }
        ],
    }


def _document(
    source_format: str = "text",
    *,
    document_meta: dict[str, Any] | None = None,
    chapter_meta: dict[str, Any] | None = None,
    segment_meta: dict[str, Any] | None = None,
    template: str | None = None,
) -> Document:
    """构造一章一段的真实 ingest 模型，供各格式策略测试复用。"""
    return Document(
        title="銀河鉄道の夜",
        source_lang="ja",
        target_lang="TOP-SECRET-TARGET-LANGUAGE",
        fmt=source_format,
        source_path="C:/TOP-SECRET-RUNTIME-PATH/book.txt",
        chapters=[
            Chapter(
                index=0,
                title="第一章",
                href="chapter.xhtml" if source_format == "epub" else None,
                template=template,
                meta=chapter_meta or {},
                segments=[
                    Segment(
                        index=0,
                        source="本文です",
                        kind="text",
                        anchor="tn0_0",
                        resource_href=("chapter.xhtml" if source_format == "epub" else None),
                        meta=segment_meta or {},
                    )
                ],
            )
        ],
        meta=document_meta or {},
    )


def _format_document(source_format: str) -> Document:
    """按当前五个读取器族的真实 metadata 字段构造代表性输入。"""
    if source_format == "text":
        return _document("text", chapter_meta={"heading_level": 2})
    if source_format in {"html", "pdf"}:
        return _document(
            source_format,
            document_meta={
                "chapter_tags": ["h3", "h1", "h2"],
                "head_html": '<meta charset="utf-8"/>',
            },
            segment_meta={
                "epub_inline": _inline_meta(),
                "epub_annotations": _annotation_meta(),
            },
            template='<p data-tn-id="tn0_0">本文です</p>',
        )
    if source_format == "fb2":
        return _document(
            "fb2",
            document_meta={
                "fb2_resources": [{"id": "cover", "content_type": "image/jpeg"}],
                "fb2_cover_image": "cover",
            },
            chapter_meta={"fb2_images": [{"id": "cover", "position": 0}]},
        )
    if source_format == "epub":
        return _document(
            "epub",
            document_meta={
                "epub_schema": 5,
                "opf_path": "content.opf",
                "toc_paths": ["nav.xhtml"],
                "toc_entries": [],
                "epub_resources": [{"index": 0, "href": "chapter.xhtml"}],
                "epub_split_strategy": "spine-fallback",
                "epub_split_toc_path": "",
                "epub_annotation_contexts": {"version": 1, "contexts": {}},
            },
            chapter_meta={"epub_split_strategy": "spine-fallback"},
            segment_meta={"epub_annotations": _annotation_meta()},
        )
    raise AssertionError(f"unsupported test format: {source_format}")


def test_public_api_requires_explicit_source_identity_only() -> None:
    """适配器参数不能重新引入路径、目标语言或含糊的 source name。"""
    expected = ["document", "source_sha256", "source_format"]

    assert list(inspect.signature(normalized_document_v1_from_ingest).parameters) == expected
    assert list(inspect.signature(encode_ingest_document_v1).parameters) == expected


def test_text_mapping_has_exact_source_only_shape_and_detached_values() -> None:
    """普通文本映射保留源结构，同时完全排除运行时和目标侧字段。"""
    document = _format_document("text")

    normalized = normalized_document_v1_from_ingest(
        document,
        source_sha256=SOURCE_HASH,
        source_format="text",
    )

    assert normalized == {
        "schema_version": NORMALIZED_DOCUMENT_SCHEMA_VERSION,
        "source_sha256": SOURCE_HASH,
        "source_format": "text",
        "source_lang": "ja",
        "title": "銀河鉄道の夜",
        "chapters": [
            {
                "index": 0,
                "title": "第一章",
                "href": None,
                "template": None,
                "meta": {"heading_level": 2},
                "segments": [
                    {
                        "index": 0,
                        "source": "本文です",
                        "kind": "text",
                        "anchor": "tn0_0",
                        "resource_href": None,
                        "cont": False,
                        "meta": {},
                    }
                ],
            }
        ],
        "meta": {},
    }

    # Mutating either side must not create a hidden shared durable value.
    document.chapters[0].meta["heading_level"] = 6
    normalized["chapters"][0]["meta"]["heading_level"] = 4
    again = normalized_document_v1_from_ingest(
        _format_document("text"),
        source_sha256=SOURCE_HASH,
        source_format="text",
    )
    assert again["chapters"][0]["meta"] == {"heading_level": 2}


@pytest.mark.parametrize("source_format", ["text", "html", "pdf", "fb2", "epub"])
def test_each_reader_family_preserves_only_its_source_metadata(source_format: str) -> None:
    """每个读取器族的现有重建字段都能通过对应的显式白名单。"""
    document = _format_document(source_format)

    payload = encode_ingest_document_v1(
        document,
        source_sha256=SOURCE_HASH,
        source_format=source_format,
    )
    decoded = decode_normalized_document_v1(payload)

    assert decoded["source_format"] == source_format
    assert (
        decoded["meta"]
        == normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format=source_format,
        )["meta"]
    )
    assert b"TOP-SECRET" not in payload
    assert b'"target"' not in payload
    assert b"source_path" not in payload
    assert b"target_lang" not in payload


@pytest.mark.parametrize("source_format", ["html", "pdf"])
def test_html_derived_formats_canonicalize_chapter_tag_order(source_format: str) -> None:
    """HTML 策略集合的插入顺序不能改变内容寻址产物的字节。"""
    first = _format_document(source_format)
    second = _format_document(source_format)
    second.meta["chapter_tags"] = ["h2", "h3", "h1", "h2"]

    first_payload = encode_ingest_document_v1(
        first,
        source_sha256=SOURCE_HASH,
        source_format=source_format,
    )
    second_payload = encode_ingest_document_v1(
        second,
        source_sha256=SOURCE_HASH,
        source_format=source_format,
    )

    assert first_payload == second_payload
    assert decode_normalized_document_v1(first_payload)["meta"]["chapter_tags"] == [
        "h1",
        "h2",
        "h3",
    ]


def test_html_reader_sorts_frozenset_policy_before_building_document(tmp_path: Path) -> None:
    """生产读取器自身也在 ingest 边界消除 frozenset 的 hash 顺序。"""
    source = tmp_path / "book.html"
    source.write_text("<html><body><h2>Two</h2><p>Body.</p></body></html>", encoding="utf-8")

    document = read_html(
        str(source),
        "en",
        "zh",
        chapter_tags=frozenset({"h3", "h1", "h2"}),
    )

    assert document.meta["chapter_tags"] == ["h1", "h2", "h3"]


class _SourceOnlyDocumentView:
    """一旦适配器读取运行时字段便立即失败的最小文档视图。"""

    def __init__(self, document: Document) -> None:
        self.title = document.title
        self.source_lang = document.source_lang
        self.fmt = document.fmt
        self.chapters = document.chapters
        self.meta = document.meta

    @property
    def source_path(self) -> str:
        """证明 source_path 不属于规范化适配器的输入。"""
        raise AssertionError("adapter must not read source_path")

    @property
    def target_lang(self) -> str:
        """证明 target_lang 不属于源文 artifact 的输入。"""
        raise AssertionError("adapter must not read target_lang")


def test_adapter_does_not_even_read_runtime_or_target_document_fields() -> None:
    """排除字段是能力边界，不只是最终 JSON 恰好没有对应键。"""
    source_only = _SourceOnlyDocumentView(_format_document("text"))

    normalized = normalized_document_v1_from_ingest(
        source_only,  # type: ignore[arg-type]
        source_sha256=SOURCE_HASH,
        source_format="text",
    )

    assert normalized["title"] == "銀河鉄道の夜"


@pytest.mark.parametrize(
    ("source_hash", "source_format", "document_format", "source_lang"),
    [
        ("A" * 64, "text", "text", "ja"),
        (SOURCE_HASH, "txt", "text", "ja"),
        (SOURCE_HASH, "text", "html", "ja"),
        (SOURCE_HASH, "text", "text", "JA"),
        (SOURCE_HASH, "text", "text", "auto"),
    ],
)
def test_identity_inputs_must_match_the_resolved_reader_output(
    source_hash: str,
    source_format: str,
    document_format: str,
    source_lang: str,
) -> None:
    """含糊别名、格式分叉和未解析语言必须在发布 artifact 前失败。"""
    document = _format_document(document_format)
    document.source_lang = source_lang

    with pytest.raises(ValueError):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=source_hash,
            source_format=source_format,
        )


@pytest.mark.parametrize("location", ["chapter", "segment"])
def test_adapter_rejects_non_positional_ingest_indexes(location: str) -> None:
    """持久化地址必须等于列表位置，不能静默重编号损坏的 ingest 输出。"""
    document = _format_document("text")
    if location == "chapter":
        document.chapters[0].index = 1
    else:
        document.chapters[0].segments[0].index = 1

    with pytest.raises(ValueError, match="index"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="text",
        )


def test_translated_segment_is_rejected_without_echoing_its_content() -> None:
    """旧流水线的译文不得伪装成新工作流的不可变源文 artifact。"""
    document = _format_document("text")
    document.chapters[0].segments[0].target = "TOP-SECRET-TRANSLATED-CONTENT"

    with pytest.raises(ValueError) as captured:
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="text",
        )

    assert "TOP-SECRET" not in str(captured.value)


@pytest.mark.parametrize("level", ["document", "chapter", "segment"])
def test_unknown_metadata_is_rejected_at_every_provenance_boundary(level: str) -> None:
    """后续阶段或插件添加的字段不能被无条件复制进源文 artifact。"""
    document = _format_document("text")
    metadata = {
        "document": document.meta,
        "chapter": document.chapters[0].meta,
        "segment": document.chapters[0].segments[0].meta,
    }[level]
    metadata["runtime_debug_path"] = "TOP-SECRET-RUNTIME-METADATA"

    with pytest.raises(ValueError) as captured:
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="text",
        )

    assert "TOP-SECRET" not in str(captured.value)


@pytest.mark.parametrize(
    ("source_format", "metadata_container"),
    [
        ("fb2", lambda document: document.meta["fb2_resources"][0]),
        ("fb2", lambda document: document.chapters[0].meta["fb2_images"][0]),
        ("epub", lambda document: document.meta["epub_resources"][0]),
    ],
)
def test_nested_resource_metadata_rejects_runtime_extensions(
    source_format: str,
    metadata_container: Any,
) -> None:
    """白名单容器内部也不能成为路径、目标结果或插件状态的逃逸通道。"""
    document = _format_document(source_format)
    nested = metadata_container(document)
    assert isinstance(nested, dict)
    nested["runtime_source_path"] = "TOP-SECRET-NESTED-RUNTIME-PATH"

    with pytest.raises(ValueError) as captured:
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format=source_format,
        )

    assert "TOP-SECRET" not in str(captured.value)


@pytest.mark.parametrize("translated_key", ["target_digest", "placements"])
def test_nested_annotation_translation_results_are_not_source_metadata(
    translated_key: str,
) -> None:
    """即使外层键合法，注释对齐结果仍属于目标侧，必须 fail closed。"""
    document = _format_document("epub")
    annotation = document.chapters[0].segments[0].meta["epub_annotations"]
    assert isinstance(annotation, dict)
    annotation[translated_key] = "digest" if translated_key == "target_digest" else []

    with pytest.raises(ValueError, match="metadata|meta|annotation"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_translated_toc_title_is_not_copied_back_into_source_metadata() -> None:
    """旧标题翻译会修改 TOC 项；该派生字段不能进入规范化源文。"""
    document = _format_document("epub")
    document.meta["toc_entries"] = [
        {
            "entry_id": "nav.xhtml:0",
            "toc_path": "nav.xhtml",
            "node_index": 0,
            "node_id": "one",
            "parent_index": None,
            "depth": 0,
            "kind": "nav",
            "title": "Chapter One",
            "raw_href": "chapter.xhtml#one",
            "resource_href": "chapter.xhtml",
            "fragment": "one",
            "target_key": "chapter.xhtml#one",
            "external": False,
            "segment_anchor": "tn0_0",
            "boundary_position": 0,
            "title_translated": "第一章",
        }
    ]

    with pytest.raises(ValueError, match="metadata|meta|toc"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_clean_import_has_no_graph_pipeline_storage_or_llm_side_effects() -> None:
    """A2 是 ingest/domain 边界，不能反向加载图、旧编排器或具体存储。"""
    script = """
import sys
import trans_novel.ingest.normalized_document_adapter

forbidden = (
    "langgraph",
    "trans_novel.cli",
    "trans_novel.llm",
    "trans_novel.pipeline",
    "trans_novel.storage",
)
loaded = sorted(name for name in sys.modules if name.startswith(forbidden))
assert not loaded, loaded
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_input_metadata_is_not_mutated_during_canonicalization() -> None:
    """HTML 标签排序和白名单复制必须保持 reader 模型归调用方所有。"""
    document = _format_document("html")
    before = deepcopy(document.model_dump())

    normalized_document_v1_from_ingest(
        document,
        source_sha256=SOURCE_HASH,
        source_format="html",
    )

    assert document.model_dump() == before
