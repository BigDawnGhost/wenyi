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


def _toc_entry() -> dict[str, object]:
    """构造 EPUB TOC reader 始终产出的十二个基础字段。"""
    return {
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
    }


def _annotation_contexts() -> dict[str, object]:
    """构造 EPUB reader 按目标键索引的最小合法注释上下文。"""
    target_key = "notes.xhtml#note-1"
    return {
        "version": 1,
        "contexts": {
            target_key: {
                "target_key": target_key,
                "resource_href": "notes.xhtml",
                "fragment": "note-1",
                "source_blocks": ["A source note."],
                "segment_anchors": ["tn1_0"],
            }
        },
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
                        # text_reader 没有结构回填锚点；其它 reader 会生成。
                        anchor=None if source_format == "text" else "tn0_0",
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
                        "anchor": None,
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


@pytest.mark.parametrize(
    ("metadata_name", "field", "invalid_value"),
    [
        ("epub_inline", "version", True),
        ("epub_inline", "source_length", -1),
        ("epub_inline", "nodes", {}),
        ("epub_annotations", "version", 2),
        ("epub_annotations", "source_length", True),
        ("epub_annotations", "items", {}),
    ],
)
def test_segment_metadata_rejects_wrong_container_version_and_length_values(
    metadata_name: str,
    field: str,
    invalid_value: object,
) -> None:
    """合法元数据键不能掩盖错误的版本、长度类型或列表容器。"""
    document = _format_document("html")
    metadata = document.chapters[0].segments[0].meta[metadata_name]
    assert isinstance(metadata, dict)
    metadata[field] = invalid_value

    with pytest.raises(ValueError, match="metadata|meta|inline|annotation"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="html",
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("id", ""),
        ("tag", []),
        ("placement", "middle"),
        ("offset", True),
        ("offset", 5),
    ],
)
def test_inline_node_rejects_invalid_reader_values(
    field: str,
    invalid_value: object,
) -> None:
    """内联节点必须保持 reader 的非空标识、位置枚举和闭区间偏移。"""
    document = _format_document("html")
    inline = document.chapters[0].segments[0].meta["epub_inline"]
    assert isinstance(inline, dict)
    nodes = inline["nodes"]
    assert isinstance(nodes, list)
    nodes[0][field] = invalid_value

    with pytest.raises(ValueError, match="metadata|meta|inline"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="html",
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("id", ""),
        ("mode", "block"),
        ("source_start", -1),
        ("source_start", True),
        ("source_end", 5),
        ("source_text", []),
        ("relation", "translation"),
    ],
)
def test_annotation_item_rejects_invalid_reader_values(
    field: str,
    invalid_value: object,
) -> None:
    """注释项只接受 reader 的源区间、字符串字段和关系枚举。"""
    document = _format_document("epub")
    annotations = document.chapters[0].segments[0].meta["epub_annotations"]
    assert isinstance(annotations, dict)
    items = annotations["items"]
    assert isinstance(items, list)
    items[0][field] = invalid_value

    with pytest.raises(ValueError, match="metadata|meta|annotation"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_annotation_item_rejects_reversed_source_range() -> None:
    """单个注释的起点不能越过终点，即使两者都在总长度以内。"""
    document = _format_document("epub")
    annotations = document.chapters[0].segments[0].meta["epub_annotations"]
    assert isinstance(annotations, dict)
    items = annotations["items"]
    assert isinstance(items, list)
    items[0]["source_start"] = 3
    items[0]["source_end"] = 2

    with pytest.raises(ValueError, match="metadata|meta|annotation"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("node_index", True),
        ("parent_index", -1),
        ("depth", -1),
        ("kind", "html"),
        ("external", 1),
    ],
)
def test_toc_entry_rejects_invalid_reader_values(field: str, invalid_value: object) -> None:
    """TOC 的索引、层级、种类和外链标志必须保留 reader 的精确类型。"""
    document = _format_document("epub")
    entry = _toc_entry()
    entry[field] = invalid_value
    document.meta["toc_entries"] = [entry]

    with pytest.raises(ValueError, match="metadata|meta|toc"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_toc_entry_requires_every_reader_base_field() -> None:
    """允许派生边界字段缺省，但 reader 固有的十二个 TOC 基础字段缺一不可。"""
    document = _format_document("epub")
    entry = _toc_entry()
    del entry["raw_href"]
    document.meta["toc_entries"] = [entry]

    with pytest.raises(ValueError, match="metadata|meta|toc"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


@pytest.mark.parametrize(
    ("mutation", "invalid_value"),
    [
        ("version", True),
        ("contexts", []),
        ("target_key", "wrong.xhtml#target"),
        ("source_blocks", "not-a-list"),
        ("segment_anchors", [1]),
    ],
)
def test_annotation_contexts_reject_invalid_values_and_index_mismatch(
    mutation: str,
    invalid_value: object,
) -> None:
    """注释上下文必须为 v1 映射，且索引键要绑定内部 target_key。"""
    document = _format_document("epub")
    contexts = _annotation_contexts()
    document.meta["epub_annotation_contexts"] = contexts
    if mutation in {"version", "contexts"}:
        contexts[mutation] = invalid_value
    else:
        context_map = contexts["contexts"]
        assert isinstance(context_map, dict)
        context = context_map["notes.xhtml#note-1"]
        assert isinstance(context, dict)
        context[mutation] = invalid_value

    with pytest.raises(ValueError, match="metadata|meta|context"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


@pytest.mark.parametrize(
    ("source_format", "metadata_path", "field", "invalid_value"),
    [
        ("fb2", "document", "id", ""),
        ("fb2", "document", "content_type", []),
        ("fb2", "chapter", "position", True),
        ("epub", "document", "index", -1),
        ("epub", "document", "href", ""),
    ],
)
def test_resource_metadata_rejects_invalid_reader_values(
    source_format: str,
    metadata_path: str,
    field: str,
    invalid_value: object,
) -> None:
    """资源清单的合法键仍须匹配 reader 的标识符和非负原生整数。"""
    document = _format_document(source_format)
    if metadata_path == "chapter":
        items = document.chapters[0].meta["fb2_images"]
    elif source_format == "fb2":
        items = document.meta["fb2_resources"]
    else:
        items = document.meta["epub_resources"]
    assert isinstance(items, list)
    items[0][field] = invalid_value

    with pytest.raises(ValueError, match="metadata|meta|resource|image"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format=source_format,
        )


@pytest.mark.parametrize(
    ("source_format", "missing_key"),
    [
        ("html", "chapter_tags"),
        ("html", "head_html"),
        ("pdf", "chapter_tags"),
        ("pdf", "head_html"),
        ("epub", "epub_schema"),
        ("epub", "opf_path"),
        ("epub", "toc_paths"),
        ("epub", "toc_entries"),
        ("epub", "epub_resources"),
        ("epub", "epub_split_strategy"),
        ("epub", "epub_split_toc_path"),
        ("epub", "epub_annotation_contexts"),
    ],
)
def test_reader_required_document_metadata_cannot_be_missing(
    source_format: str,
    missing_key: str,
) -> None:
    """适配器应拒绝不可能由对应 reader 正常产出的残缺文档元数据。"""
    document = _format_document(source_format)
    del document.meta[missing_key]

    with pytest.raises(ValueError, match="metadata|meta|required|missing"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format=source_format,
        )


@pytest.mark.parametrize(
    ("source_format", "missing_key"),
    [("text", "heading_level"), ("epub", "epub_split_strategy")],
)
def test_reader_required_chapter_metadata_cannot_be_missing(
    source_format: str,
    missing_key: str,
) -> None:
    """文本标题层级与 EPUB 切章策略是每章 reader provenance 的必填部分。"""
    document = _format_document(source_format)
    del document.chapters[0].meta[missing_key]

    with pytest.raises(ValueError, match="metadata|meta|required|missing"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format=source_format,
        )


@pytest.mark.parametrize("source_format", ["text", "fb2"])
@pytest.mark.parametrize("field", ["href", "template", "resource_href"])
def test_non_markup_reader_formats_reject_structural_field_injection(
    source_format: str,
    field: str,
) -> None:
    """Text/FB2 reader 不产生 HTML/EPUB 的 href、模板或资源归属字段。"""
    document = _format_document(source_format)
    if field == "resource_href":
        document.chapters[0].segments[0].resource_href = "injected.xhtml"
    else:
        setattr(document.chapters[0], field, "injected.xhtml")

    with pytest.raises(ValueError, match="href|template|format|structure"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format=source_format,
        )


@pytest.mark.parametrize("source_format", ["html", "pdf"])
@pytest.mark.parametrize("field", ["href", "resource_href", "missing_template"])
def test_html_derived_formats_enforce_their_structural_fields(
    source_format: str,
    field: str,
) -> None:
    """HTML/PDF 仅持有章模板，不得携带 EPUB href，且模板由 reader 必定生成。"""
    document = _format_document(source_format)
    if field == "resource_href":
        document.chapters[0].segments[0].resource_href = "injected.xhtml"
    elif field == "missing_template":
        document.chapters[0].template = None
    else:
        document.chapters[0].href = "injected.xhtml"

    with pytest.raises(ValueError, match="href|template|format|structure"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format=source_format,
        )


@pytest.mark.parametrize("field", ["href", "template", "resource_href"])
def test_epub_format_enforces_resource_owned_structural_fields(field: str) -> None:
    """EPUB 章和段必须绑定资源 href，同时不得回退到旧 HTML 模板。"""
    document = _format_document("epub")
    if field == "href":
        document.chapters[0].href = None
    elif field == "template":
        document.chapters[0].template = "<p>injected legacy template</p>"
    else:
        document.chapters[0].segments[0].resource_href = None

    with pytest.raises(ValueError, match="href|template|format|structure"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_optional_reader_metadata_and_toc_derived_fields_remain_optional() -> None:
    """严格校验不得把 FB2 可选信息或 TOC 后续定位派生字段误设为必填。"""
    fb2 = _format_document("fb2")
    fb2.meta = {}
    fb2.chapters[0].meta = {}
    epub = _format_document("epub")
    epub.meta["toc_entries"] = [_toc_entry()]

    normalized_document_v1_from_ingest(
        fb2,
        source_sha256=SOURCE_HASH,
        source_format="fb2",
    )
    normalized_document_v1_from_ingest(
        epub,
        source_sha256=SOURCE_HASH,
        source_format="epub",
    )


def test_annotation_length_may_cover_a_logical_continuation_chain() -> None:
    """长段拆分后注释长度属于完整逻辑段，不能强行等同于首个物理段长度。"""
    document = _format_document("epub")
    annotations = document.chapters[0].segments[0].meta["epub_annotations"]
    assert isinstance(annotations, dict)
    annotations["source_length"] = 8
    document.chapters[0].segments.append(
        Segment(
            index=1,
            source="続きを読む",
            anchor=None,
            resource_href="chapter.xhtml",
            cont=True,
        )
    )

    normalized_document_v1_from_ingest(
        document,
        source_sha256=SOURCE_HASH,
        source_format="epub",
    )


@pytest.mark.parametrize("invalid_schema", [True, 5.0])
def test_epub_schema_requires_the_native_reader_version(invalid_schema: object) -> None:
    """数值相等不代表类型相同；EPUB reader 的版本字段必须是原生整数 5。"""
    document = _format_document("epub")
    document.meta["epub_schema"] = invalid_schema

    with pytest.raises(ValueError, match="metadata|meta|schema"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


@pytest.mark.parametrize(
    ("metadata_name", "field"),
    [("epub_inline", "placement"), ("epub_annotations", "mode")],
)
def test_unhashable_segment_enum_values_raise_the_safe_adapter_error(
    metadata_name: str,
    field: str,
) -> None:
    """错误容器传入枚举字段时应稳定拒绝，而不是泄漏内部 TypeError。"""
    document = _format_document("html")
    metadata = document.chapters[0].segments[0].meta[metadata_name]
    assert isinstance(metadata, dict)
    items = metadata["nodes" if metadata_name == "epub_inline" else "items"]
    assert isinstance(items, list)
    items[0][field] = []

    with pytest.raises(ValueError, match="metadata|meta|inline|annotation"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="html",
        )


@pytest.mark.parametrize(
    ("location", "invalid_path"),
    [
        ("chapter", "   "),
        ("segment", "../outside.xhtml"),
        ("resource", "/absolute.xhtml"),
        ("opf", "https://example.invalid/book.opf"),
        ("split_toc", "../nav.xhtml"),
        ("split_toc", "/nav.xhtml"),
        ("toc_entry", "/nav.xhtml"),
        ("toc_entry", "../nav.xhtml"),
    ],
)
def test_epub_structural_paths_are_nonblank_package_relative_paths(
    location: str,
    invalid_path: str,
) -> None:
    """EPUB reader 的定位字段必须留在包内，不能成为外部路径注入通道。"""
    document = _format_document("epub")
    if location == "chapter":
        document.chapters[0].href = invalid_path
    elif location == "segment":
        document.chapters[0].segments[0].resource_href = invalid_path
    elif location == "resource":
        resources = document.meta["epub_resources"]
        assert isinstance(resources, list)
        resources[0]["href"] = invalid_path
    elif location == "opf":
        document.meta["opf_path"] = invalid_path
    elif location == "split_toc":
        document.meta["epub_split_toc_path"] = invalid_path
    else:
        entry = _toc_entry()
        entry["toc_path"] = invalid_path
        document.meta["toc_entries"] = [entry]

    with pytest.raises(ValueError, match="metadata|meta|href|path|structure"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_empty_epub_split_toc_path_remains_valid_for_spine_fallback() -> None:
    """没有可用 TOC 边界时 reader 用空字符串表示回退，不应被路径校验误拒。"""
    document = _format_document("epub")
    document.meta["epub_split_toc_path"] = ""

    normalized_document_v1_from_ingest(
        document,
        source_sha256=SOURCE_HASH,
        source_format="epub",
    )


def test_annotation_context_target_key_must_match_its_resource_and_fragment() -> None:
    """映射键与内部键即使相等，也不能伪装成另一个资源和 fragment 的上下文。"""
    document = _format_document("epub")
    contexts = _annotation_contexts()
    context_map = contexts["contexts"]
    assert isinstance(context_map, dict)
    context = context_map.pop("notes.xhtml#note-1")
    assert isinstance(context, dict)
    context["target_key"] = "other.xhtml#note-1"
    context_map["other.xhtml#note-1"] = context
    document.meta["epub_annotation_contexts"] = contexts

    with pytest.raises(ValueError, match="metadata|meta|context|target"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_internal_toc_target_key_must_match_its_resource_and_fragment() -> None:
    """非外链 TOC 的稳定目标键必须由同项的包内资源与 fragment 唯一确定。"""
    document = _format_document("epub")
    entry = _toc_entry()
    entry["target_key"] = "other.xhtml#one"
    document.meta["toc_entries"] = [entry]

    with pytest.raises(ValueError, match="metadata|meta|toc|target"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_text_reader_segments_cannot_inject_reconstruction_anchors() -> None:
    """Text reader 从不生成回填锚点，不能接受其它 reader 的结构标识注入。"""
    document = _format_document("text")
    document.chapters[0].segments[0].anchor = "tn0_0"

    with pytest.raises(ValueError, match="anchor|format|structure"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="text",
        )


@pytest.mark.parametrize(
    ("placement", "offset"),
    [
        ("before", 1),
        ("after", 0),
        ("after", 3),
        ("inline", 0),
        ("inline", 4),
    ],
)
def test_inline_placement_must_match_its_source_offset(
    placement: str,
    offset: int,
) -> None:
    """before/after/inline 必须分别绑定段首、段尾和严格内部偏移。"""
    document = _format_document("html")
    inline = document.chapters[0].segments[0].meta["epub_inline"]
    assert isinstance(inline, dict)
    nodes = inline["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["placement"] = placement
    nodes[0]["offset"] = offset

    with pytest.raises(ValueError, match="metadata|meta|inline|placement|offset"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="html",
        )


def test_all_reader_inline_placement_forms_are_accepted() -> None:
    """reader 的段首、段中和段尾三种合法节点位置均应跨过适配边界。"""
    document = _format_document("html")
    inline = document.chapters[0].segments[0].meta["epub_inline"]
    assert isinstance(inline, dict)
    inline["nodes"] = [
        {"id": "tn0_0_inline_0", "tag": "img", "placement": "before", "offset": 0},
        {"id": "tn0_0_inline_1", "tag": "img", "placement": "inline", "offset": 2},
        {"id": "tn0_0_inline_2", "tag": "img", "placement": "after", "offset": 4},
    ]

    normalized_document_v1_from_ingest(
        document,
        source_sha256=SOURCE_HASH,
        source_format="html",
    )


def test_point_annotation_requires_a_zero_length_source_range() -> None:
    """point 注释是段落边界事件，不能伪装成覆盖正文字符的 range。"""
    document = _format_document("epub")
    annotations = document.chapters[0].segments[0].meta["epub_annotations"]
    assert isinstance(annotations, dict)
    items = annotations["items"]
    assert isinstance(items, list)
    items[0]["source_start"] = 0
    items[0]["source_end"] = 1
    items[0]["source_text"] = "本"

    with pytest.raises(ValueError, match="metadata|meta|annotation|point|range"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


@pytest.mark.parametrize("mode", ["point", "range"])
def test_zero_length_annotation_range_remains_valid(mode: str) -> None:
    """point 必须零长度；合同同时保留损坏但可表示的零长度 range。"""
    document = _format_document("epub")
    annotations = document.chapters[0].segments[0].meta["epub_annotations"]
    assert isinstance(annotations, dict)
    items = annotations["items"]
    assert isinstance(items, list)
    items[0]["mode"] = mode
    items[0]["source_start"] = 2
    items[0]["source_end"] = 2
    items[0]["source_text"] = ""

    normalized_document_v1_from_ingest(
        document,
        source_sha256=SOURCE_HASH,
        source_format="epub",
    )


def test_inline_node_ids_must_be_unique_within_a_segment() -> None:
    """重复内联 ID 会让 writer 多次命中同一 DOM 节点并静默丢失后续记录。"""
    document = _format_document("html")
    inline = document.chapters[0].segments[0].meta["epub_inline"]
    assert isinstance(inline, dict)
    inline["nodes"] = [
        {"id": "duplicate", "tag": "img", "placement": "before", "offset": 0},
        {"id": "duplicate", "tag": "img", "placement": "after", "offset": 4},
    ]

    with pytest.raises(ValueError, match="metadata|meta|inline|duplicate|unique"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="html",
        )


def test_annotation_item_ids_must_be_unique_within_a_segment() -> None:
    """重复注释 ID 会把多个源项折叠为一个对齐/DOM 恢复身份。"""
    document = _format_document("epub")
    annotations = document.chapters[0].segments[0].meta["epub_annotations"]
    assert isinstance(annotations, dict)
    items = annotations["items"]
    assert isinstance(items, list)
    items.append(deepcopy(items[0]))

    with pytest.raises(ValueError, match="metadata|meta|annotation|duplicate|unique"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


def test_toc_path_and_node_index_pairs_must_be_unique() -> None:
    """writer 以 toc_path+node_index 建索引，重复地址会 last-write-wins 回填错项。"""
    document = _format_document("epub")
    first = _toc_entry()
    second = _toc_entry()
    second["entry_id"] = "nav.xhtml:duplicate"
    second["title"] = "Different source title"
    document.meta["toc_entries"] = [first, second]

    with pytest.raises(ValueError, match="metadata|meta|toc|duplicate|unique"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )


@pytest.mark.parametrize("duplicate_field", ["index", "href"])
def test_epub_resource_addresses_must_be_unique(duplicate_field: str) -> None:
    """资源 index/href 均由 reader 唯一分配，重复会在 writer 中覆盖或静默跳过。"""
    document = _format_document("epub")
    resources = document.meta["epub_resources"]
    assert isinstance(resources, list)
    duplicate = {"index": 1, "href": "second.xhtml"}
    duplicate[duplicate_field] = resources[0][duplicate_field]
    resources.append(duplicate)

    with pytest.raises(ValueError, match="metadata|meta|resource|duplicate|unique"):
        normalized_document_v1_from_ingest(
            document,
            source_sha256=SOURCE_HASH,
            source_format="epub",
        )
