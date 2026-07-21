"""Complete English model-facing language bundle."""

from ..common.registry import LanguageBundle
from . import formatters, punctuation
from .policy import (
    default_person_type,
    default_term_type,
    language_label,
    pair_guidance,
    source_guidance,
    target_profile,
    term_guidance,
)
from .prompts import TEMPLATES


BUNDLE = LanguageBundle(
    templates=TEMPLATES,
    target_profile=target_profile,
    language_label=language_label,
    source_guidance=source_guidance,
    pair_guidance=pair_guidance,
    term_guidance=term_guidance,
    default_term_type=default_term_type,
    default_person_type=default_person_type,
    formatters=formatters,
    punctuation=punctuation,
)

__all__ = ["BUNDLE"]
