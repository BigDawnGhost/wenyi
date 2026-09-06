"""Review agent using the cheap tier.
Compare source and translation paragraph by paragraph for omissions, additions,
mistranslations, glossary violations and pronoun errors.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..llm.json_parser import parse_json_result
from . import prompts
from .base import Agent


class ReviewOutputError(ValueError):
    """Structured review output error that can be retried with a smaller input."""

    def __init__(self, reason: str):
        super().__init__(f"Review output protocol error: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class ReviewResult:
    """Structured result of one review call, including whether local JSON repair was used."""

    issues: list[dict[str, Any]]
    repaired: bool = False


class Reviewer(Agent):
    def review(
        self, sources: list[str], targets: list[str], glossary_terms=None
    ) -> list[dict[str, Any]]:
        """Return issue dictionaries containing index, type, detail and suggestion."""
        return self.review_result(sources, targets, glossary_terms).issues

    def review_result(
        self,
        sources: list[str],
        targets: list[str],
        glossary_terms=None,
        *,
        trace: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> ReviewResult:
        """Return issues with recovery metadata; leave service exceptions to the caller."""
        if not sources:
            return ReviewResult([])
        system = prompts.render("reviewer_system", src=self.src, tgt=self.tgt, n=len(sources))
        user = prompts.render(
            "reviewer_user",
            src=self.src,
            tgt=self.tgt,
            glossary=prompts.render_glossary(glossary_terms or []),
            n=len(sources),
            pairs=prompts.numbered_pairs(sources, targets),
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if trace:
            trace("request", {"messages": [dict(message) for message in messages]})
        try:
            text = self.client.complete(
                messages,
                tier="cheap",
                json_mode=True,
                stage=type(self).__name__,
            )
        except Exception as error:
            if trace:
                trace(
                    "error",
                    {
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                )
            raise
        if trace:
            trace("response", {"raw_response": text})
        try:
            parsed = parse_json_result(text)
        except ValueError:
            raise ReviewOutputError("malformed_json") from None
        data = parsed.value
        repaired = parsed.repaired
        if trace:
            trace(
                "parsed",
                {
                    "value": data,
                    "json_repaired": repaired,
                },
            )

        if not isinstance(data, dict):
            raise ReviewOutputError("response_not_object")
        if list(data)[-2:] != ["reviewed_segments", "complete"]:
            raise ReviewOutputError("completion_footer_not_last")
        reviewed_segments = data.get("reviewed_segments")
        if (
            isinstance(reviewed_segments, bool)
            or not isinstance(reviewed_segments, int)
            or reviewed_segments != len(sources)
        ):
            raise ReviewOutputError("reviewed_segments_mismatch")
        if data.get("complete") is not True:
            raise ReviewOutputError("completion_marker_missing")

        issues = data.get("issues")
        if not isinstance(issues, list):
            raise ReviewOutputError("issues_not_list")
        if any(not isinstance(item, dict) for item in issues):
            raise ReviewOutputError("issue_not_object")
        validated = self._validate_issues(issues, len(sources))
        return ReviewResult(validated, repaired=repaired)

    @staticmethod
    def _validate_issues(
        issues: list[dict[str, Any]],
        segment_count: int,
    ) -> list[dict[str, Any]]:
        """Normalize and validate every candidate so malformed fields cannot silently mean no
        issues.
        """
        allowed_types = {
            "missing",
            "added",
            "mistranslation",
            "terminology",
            "pronoun",
        }
        for item in issues:
            index = item.get("index")
            if isinstance(index, str):
                try:
                    index = int(index.strip())
                except ValueError:
                    index = None
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < segment_count
            ):
                raise ReviewOutputError("invalid_issue_index")
            if item.get("type") not in allowed_types:
                raise ReviewOutputError("invalid_issue_type")
            for field in ("detail", "suggestion"):
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ReviewOutputError(f"invalid_issue_{field}")
        return [
            {
                "index": int(str(item["index"]).strip()),
                "type": item["type"],
                "detail": item["detail"].strip(),
                "suggestion": item["suggestion"].strip(),
            }
            for item in issues
        ]
