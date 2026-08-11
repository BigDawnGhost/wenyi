"""纯文档采样服务的多点、纯文本和短书回退合同。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from trans_novel.services import sample_document_text


@dataclass
class _Segment:
    """只实现 source 采样端口的测试段落。"""

    source: str


@dataclass
class _Chapter:
    """提供与生产 Chapter 同形状的有序正文段落。"""

    text_segments: list[_Segment] = field(default_factory=list)


@dataclass
class _Document:
    """提供与生产 Document 同形状的章节序列。"""

    chapters: list[_Chapter] = field(default_factory=list)


def _document(*chapter_texts: str) -> _Document:
    """从每章一段的源文快速构造采样视图。"""
    return _Document([_Chapter([_Segment(text)]) for text in chapter_texts])


def test_labeled_sample_uses_front_middle_and_tail_in_book_order() -> None:
    """三点样本前两块取开头，末块取结尾，并保留固定标签顺序。"""
    opening = "OPEN-" + "a" * 3000
    middle = "MIDDLE-" + "b" * 3000
    ending = "c" * 3000 + "-ENDING"

    sample = sample_document_text(_document(opening, middle, ending))

    opening_pos = sample.index("【开头样章】")
    middle_pos = sample.index("【中部样章】")
    ending_pos = sample.index("【结尾样章】")
    assert opening_pos < middle_pos < ending_pos
    assert "OPEN-" in sample
    assert "MIDDLE-" in sample
    assert "-ENDING" in sample
    assert [len(part.split("\n", 1)[1]) for part in sample.split("\n\n")] == [2800, 2800, 2800]


def test_plain_sample_uses_first_long_chapter_without_labels() -> None:
    """语言检测只取第一篇足够长章节并限制为 6000 字符。"""
    first = "FIRST-" + "a" * 7000
    second = "SECOND-" + "b" * 500

    sample = sample_document_text(_document("short", first, second), labeled=False)

    assert sample.startswith("FIRST-")
    assert len(sample) == 6000
    assert "SECOND-" not in sample
    assert "样章】" not in sample


def test_all_short_chapters_fall_back_to_first_two_without_labels() -> None:
    """没有超过阈值的章节时只拼前两章，第三章不进入任何模式。"""
    document = _document("first", "second", "third")

    assert sample_document_text(document) == "first\nsecond"
    assert sample_document_text(document, labeled=False) == "first\nsecond"


def test_empty_document_returns_empty_text() -> None:
    """空文档不制造标签或占位符，供上层语言准入判断无样本错误。"""
    assert sample_document_text(_Document()) == ""
    assert sample_document_text(_Document(), labeled=False) == ""


def test_short_book_fallback_flattens_segments_without_empty_chapter_separator() -> None:
    """旧短书路径平铺前两章的段落，空章节本身不会制造额外换行。"""
    document = _Document(
        [
            _Chapter(),
            _Chapter([_Segment("first segment"), _Segment("second segment")]),
            _Chapter([_Segment("ignored")]),
        ]
    )

    assert sample_document_text(document) == "first segment\nsecond segment"


def test_one_or_two_long_chapters_are_not_sampled_twice() -> None:
    """采样位置重合时保留最先出现的标签，不复制同一章正文。"""
    one = sample_document_text(_document("a" * 201))
    two = sample_document_text(_document("a" * 201, "b" * 201))

    assert one.count("样章】") == 1
    assert "【开头样章】" in one
    assert two.count("样章】") == 2
    assert "【开头样章】" in two
    assert "【中部样章】" in two
    assert "【结尾样章】" not in two


def test_threshold_is_strictly_greater_than_two_hundred_characters() -> None:
    """恰好 200 字仍走短章回退，201 字才进入带标签多点采样。"""
    assert "样章】" not in sample_document_text(_document("a" * 200))
    assert "【开头样章】" in sample_document_text(_document("a" * 201))


def test_returned_text_is_detached_from_later_document_mutation() -> None:
    """字符串样本在调用结束后不受段落对象变更影响。"""
    document = _document("stable-" + "a" * 300)

    sample = sample_document_text(document, labeled=False)
    document.chapters[0].text_segments[0].source = "mutated"

    assert sample.startswith("stable-")


@pytest.mark.parametrize("labeled", [None, 0, 1, "yes"])
def test_labeled_mode_must_be_boolean(labeled: object) -> None:
    """公共采样模式拒绝依赖 Python truthiness 的含糊输入。"""
    with pytest.raises(TypeError, match="labeled"):
        sample_document_text(_document("text"), labeled=labeled)  # type: ignore[arg-type]


def test_invalid_document_view_is_rejected_with_location() -> None:
    """错误提示应定位到缺失的章节或段落字段，而不是产生 AttributeError。"""
    with pytest.raises(TypeError, match="document.chapters"):
        sample_document_text(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"text_segments\[0\]\.source"):
        sample_document_text(_Document([_Chapter([object()])]))  # type: ignore[list-item]
