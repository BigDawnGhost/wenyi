"""Dynamic model-input formatting for the English prompt bundle."""

from __future__ import annotations

from typing import Any


EMPTY = "(none)"
SAMPLE_LABELS = ("Opening sample", "Middle sample", "Closing sample")


def render_glossary(terms: list[Any]) -> str:
    """Render stored glossary fields verbatim with English metadata labels."""
    if not terms:
        return "(none)"
    lines: list[str] = []
    for term in terms:
        extra = [term.type]
        if term.gender:
            extra.append(term.gender)
        if term.reading:
            extra.append(f"reading: {term.reading}")
        tag = f"({', '.join(extra)})"
        aliases = f" [aliases: {', '.join(term.aliases)}]" if term.aliases else ""
        note = f" [note: {term.note}]" if term.note else ""
        lines.append(f"- {term.source} → {term.target}{tag}{aliases}{note}")
    return "\n".join(lines)


def style_brief(analysis: dict[str, Any], characters: list[dict[str, Any]]) -> str:
    """Render analysis fields as an English style and character brief."""
    labels = (
        ("genre", "Genre"),
        ("tone", "Tone and style"),
        ("style_guide", "Style guide"),
        ("narration", "Narration"),
        ("pacing", "Pacing and sentence rhythm"),
        ("register", "Register"),
        ("dialogue_style", "Dialogue style"),
        ("rhetoric", "Rhetoric"),
    )
    lines = [f"{label}: {analysis[key]}" for key, label in labels if analysis.get(key)]
    if characters:
        lines.append("Characters:")
        for character in characters:
            metadata = [str(character.get("source", ""))]
            if character.get("gender"):
                metadata.append(str(character["gender"]))
            if character.get("note"):
                metadata.append(str(character["note"]))
            target = character.get("target", character.get("source", ""))
            lines.append(f"  - {target} ({', '.join(metadata)})")
    return "\n".join(lines)


def numbered_pairs(sources: list[str], targets: list[str]) -> str:
    """Render indexed source/translation pairs for review."""
    return "\n".join(
        f"[{index}] Source: {source}\n    Translation: {target}"
        for index, (source, target) in enumerate(zip(sources, targets))
    )


def backtranslation_pairs(sources: list[str], backtranslations: list[str]) -> str:
    """Render indexed source/backtranslation pairs for fidelity checking."""
    return "\n".join(
        f"[{index}] Source: {source}\n    Backtranslation: {backtranslation}"
        for index, (source, backtranslation) in enumerate(
            zip(sources, backtranslations)
        )
    )


def chapter_label(title: str, index: int) -> str:
    """Return an English fallback label for a chapter excerpt."""
    return title.strip() or f"Chapter {index + 1}"


def chapter_digest(label: str, snippet: str) -> str:
    """Render one chapter excerpt for the consistency prompt."""
    return f"[{label}]\n{snippet}"


def join_digest_segments(segments: list[str]) -> str:
    """Join head and tail excerpts with an English ellipsis separator."""
    return " … ".join(segments)


def autofix_feedback(issues: list[dict[str, Any]]) -> str:
    """Render reviewer issues as English targeted-retranslation feedback."""
    return "; ".join(
        f"{issue.get('detail', '')} (Suggested correction: "
        f"{issue.get('suggestion', '')})"
        for issue in issues
    )


def sample_block(label: str, text: str) -> str:
    """Render a position-labeled sample for style analysis."""
    return f"[{label}]\n{text}"
