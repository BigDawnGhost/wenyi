"""Resolve subtitle export identity without introducing book-only operations."""

from __future__ import annotations

from ..config import Config
from ..i18n.policy.models import PolicyContext, PolicyPlan, content_hash
from ..i18n.policy.resolver import resolve_policy
from ..ingest.srt_reader import SrtCue


def export_plan(
    config: Config,
    manifest: dict,
    cues: list[SrtCue],
    translations: dict[str, str],
    *,
    bilingual: bool,
) -> PolicyPlan:
    """Bind the actual writer inputs; SRT has fixed ordering and no about page or styles."""
    identity = content_hash(
        {
            "source_sha256": manifest.get("source_sha256"),
            "cues": [
                (cue.index, cue.timestamp, cue.text, translations.get(cue.index)) for cue in cues
            ],
        }
    )
    return resolve_policy(
        PolicyContext(
            manifest.get("source_lang", config.source_lang),
            manifest.get("target_lang", config.target_lang),
            phase="export",
            path="srt",
            format="srt",
            source_identity=identity,
            bilingual=bilingual,
            about_page=False,
        ),
    )
