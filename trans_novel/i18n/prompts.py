"""Render task templates and language rules with language-independent JSON keys."""

from __future__ import annotations

import re
from functools import lru_cache
from string import Template

from . import languages
from .resources import read_text


@lru_cache(maxsize=None)
def template(name: str) -> Template:
    if not re.fullmatch(r"[a-z_]+", name):
        raise ValueError(f"Invalid prompt name: {name}")
    return Template(read_text(f"tasks/{name}.txt"))


def render(name: str, *, src: str = "ja", tgt: str = "zh", **kwargs) -> str:
    src = languages.require_language(src, allow_auto=True)
    target = languages.profile(tgt)
    kwargs.setdefault("src_label", languages.label(src))
    kwargs.setdefault("tgt_label", target["label"])
    kwargs.setdefault("target_language", target["english_name"])
    kwargs.setdefault("source_language", languages.label(src))
    kwargs.setdefault("lang_guidance", languages.translate_guidance(src, tgt=tgt))
    kwargs.setdefault("target_guidance", target["target_guidance"])
    kwargs.setdefault("term_guidance", languages.term_guidance(src))
    kwargs.setdefault("punct_rule", target["punctuation_rule"])
    kwargs.setdefault("title_rule", target["title_rule"])
    kwargs.setdefault("digest_length", target["digest_length"])
    kwargs.setdefault("synopsis_length", target["synopsis_length"])
    kwargs.setdefault("review_evidence_tools", read_text("shared/review_evidence_tools.txt"))
    kwargs.setdefault(
        "metadata_guidance",
        Template(read_text("shared/metadata_guidance.txt")).substitute(
            tgt_label=target["english_name"]
        ),
    )
    # Substitute once: literal $ and braces in content stay intact; missing arguments fail.
    try:
        return template(name).substitute(**kwargs)
    except KeyError as error:
        raise ValueError(f"Prompt {name} is missing argument: {error.args[0]}") from error
