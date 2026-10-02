"""Render task templates and language rules with language-independent JSON keys."""

from __future__ import annotations

import re
from functools import lru_cache
from string import Template

from . import languages
from .policy.models import PolicyContext, PolicyPlan
from .policy.resolver import resolve_policy
from .resources import read_text

PROMPT_OPERATIONS = ("prompt.language_rules",)


@lru_cache(maxsize=None)
def template(name: str) -> Template:
    if not re.fullmatch(r"[a-z_]+", name):
        raise ValueError(f"Invalid prompt name: {name}")
    return Template(read_text(f"tasks/{name}.txt"))


def render(
    name: str, *, src: str = "ja", tgt: str = "zh", plan: PolicyPlan | None = None, **kwargs
) -> str:
    src = languages.require_language(src, allow_auto=True)
    if plan is None:
        plan = resolve_policy(PolicyContext(src, tgt))
    if plan.context.source != src or plan.context.target != languages.require_language(tgt):
        raise ValueError("Prompt language policy does not match the requested direction")
    values = dict(plan.prompt_values)
    if name in {"translator_system", "review_fixer_system"}:
        values["lang_guidance"] = values.pop("configured_lang_guidance")
    for key, value in values.items():
        kwargs.setdefault(key, value)
    frozen_templates = dict(plan.templates)
    selected = Template(frozen_templates[name]) if name in frozen_templates else template(name)
    # Substitute once: literal $ and braces in content stay intact; missing arguments fail.
    try:
        return selected.substitute(**kwargs)
    except KeyError as error:
        raise ValueError(f"Prompt {name} is missing argument: {error.args[0]}") from error
