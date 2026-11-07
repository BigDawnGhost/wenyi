"""Compose genre fragments before language substitution and policy fingerprinting."""

from __future__ import annotations

import re
from string import Template
from typing import Literal, cast, get_args

from .resources import read_json

Genre = Literal["literary", "academic"]
DEFAULT_GENRE: Genre = "literary"


def require_genre(genre: str) -> Genre:
    """Reject unsupported selections before composing or freezing any resources."""
    if genre not in get_args(Genre):
        raise ValueError(f"Unsupported prompt genre: {genre!r}; choose literary / academic")
    return cast(Genre, genre)


def compose(text: str, *, genre: str = DEFAULT_GENRE) -> str:
    """Fill only genre slots in trusted resources, leaving all other placeholders literal.

    Literary fragments are the complete defaults. Other genres override those fragments,
    including with empty strings when the literary instruction does not apply.
    """
    if not isinstance(genre, str) or not re.fullmatch(r"[a-z][a-z_]*", genre):
        raise ValueError(f"Invalid prompt genre: {genre}")
    if "${genre_" not in text and "$genre_" not in text:
        return text
    fragments = read_json(f"genres/{DEFAULT_GENRE}/fragments.json")
    if genre != DEFAULT_GENRE:
        overrides = read_json(f"genres/{genre}/fragments.json")
        unknown = overrides.keys() - fragments.keys()
        if unknown:
            raise ValueError(f"Unknown genre fragments: {', '.join(sorted(unknown))}")
        fragments.update(overrides)
    for key, value in fragments.items():
        if not re.fullmatch(r"genre_[a-z_]+", key) or not isinstance(value, str):
            raise ValueError(f"Invalid genre fragment: {key}")
        if "${genre_" in value or "$genre_" in value:
            raise ValueError(f"Nested genre fragment: {key}")

    def substitute(match: re.Match[str]) -> str:
        key = match.group("named") or match.group("braced")
        if key and key.startswith("genre_"):
            if key not in fragments:
                raise ValueError(f"Missing genre fragment: {key}")
            return fragments[key]
        return match.group(0)

    return Template.pattern.sub(substitute, text)
