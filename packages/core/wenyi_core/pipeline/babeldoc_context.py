"""Read-only prose selection for BabelDOC translation context and batch boundaries."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from itertools import groupby

from ..ingest.models import KIND_HEADING, Chapter, Segment

_SECTION_NUMBER = re.compile(r"^\d+(?:\.\d+)*\s*(?:[|:.)–—-]\s*)?")
_ABSTRACT_HEADINGS = {"abstract", "摘要"}
_AUXILIARY_HEADINGS = {
    "acknowledgments",
    "acknowledgements",
    "affiliations",
    "author contributions",
    "author information",
    "bibliography",
    "conflict of interest",
    "conflicts of interest",
    "data availability",
    "data availability statement",
    "funding",
    "literature cited",
    "references",
    "supporting information",
    "works cited",
    "参考文献",
    "作者信息",
    "利益冲突",
    "致谢",
}


def _heading_text(text: str) -> str:
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return _SECTION_NUMBER.sub("", text).strip(" .:|")


def _is_heading(segment: Segment) -> bool:
    return segment.kind == KIND_HEADING or segment.meta.get("layout_label") == "title"


@dataclass(frozen=True)
class BabeldocContext:
    """Project original text positions into section-scoped prose without renumbering.

    Layout labels are evidence, not a complete semantic model. Unknown/fallback units
    remain translatable but cannot become prose context. Recognizable academic front
    matter and auxiliary sections narrow the plain-text allowlist further.
    """

    sections: tuple[int, ...]
    body: tuple[bool, ...]
    labels: tuple[str, ...]

    @classmethod
    def for_chapter(cls, chapter: Chapter) -> BabeldocContext | None:
        segments = chapter.text_segments
        if not chapter.meta.get("babeldoc") and not any(
            segment.meta.get("babeldoc_id") for segment in segments
        ):
            return None

        headings = {
            index: _heading_text(segment.source)
            for index, segment in enumerate(segments)
            if _is_heading(segment)
        }
        # Only infer an article preamble when an explicit abstract anchors its end.
        body_start = (
            next(
                (index for index, title in headings.items() if title in _ABSTRACT_HEADINGS),
                0,
            )
            if chapter.index == 0
            else 0
        )
        chapter_title = _heading_text(chapter.meta.get("pdf_toc_title") or chapter.title)
        # Page-based chapters can begin with the preceding section's final paragraphs.
        # Prefer the actual heading position over assigning its whole page the TOC role.
        auxiliary = chapter_title in _AUXILIARY_HEADINGS and chapter_title not in headings.values()
        section = 0
        sections: list[int] = []
        body: list[bool] = []
        labels: list[str] = []
        for index, segment in enumerate(segments):
            label = segment.meta.get("layout_label")
            if index in headings:
                section += 1
                auxiliary = headings[index] in _AUXILIARY_HEADINGS
            sections.append(section)
            body.append(
                index >= body_start
                and index not in headings
                and label == "plain text"
                and not auxiliary
            )
            labels.append(str(label or "unknown"))
        return cls(tuple(sections), tuple(body), tuple(labels))

    def groups(self, segments: list[Segment]) -> list[list[Segment]]:
        """Keep contiguous roles and sections apart before token/completion batching."""
        keys = [
            (section, "body" if body else label, body)
            for section, body, label in zip(self.sections, self.body, self.labels)
        ]
        return [
            [segments[index] for index in indices]
            for _, indices in groupby(range(len(segments)), key=keys.__getitem__)
        ]

    def next_source(self, segments: list[Segment], end: int) -> str:
        """Find the next prose unit, skipping layout noise but never a section boundary."""
        if end <= 0 or not self.body[end - 1]:
            return ""
        section = self.sections[end - 1]
        for index in range(end, len(segments)):
            if self.sections[index] != section:
                break
            if self.body[index]:
                return segments[index].source
        return ""

    def recent_targets(self, segments: list[Segment], end: int) -> list[str]:
        """Rebuild only the consumed section prefix from formal, possibly aligned targets."""
        if end <= 0:
            return []
        section = self.sections[end - 1]
        return [
            segment.target
            for index, segment in enumerate(segments[:end])
            if self.sections[index] == section
            and self.body[index]
            and segment.target
            and segment.target.strip()
        ]
