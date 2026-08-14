"""Infrastructure-neutral planning and execution for assembled book outputs.

The caller owns the consistency boundary: it must provide either a stable live
store or an immutable export snapshot and hold any required writer lock.  This
module deliberately does not acquire locks, refresh state, validate source
identity, or publish lifecycle events.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

StoreT = TypeVar("StoreT")
ProgressFn = Callable[[int, int, str], None]
Renderer = Callable[..., str]
StageCall = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class PublishingOptions:
    """Output variants and presentation options for one assemble operation."""

    mono: bool
    bilingual: bool
    bilingual_order: str
    bilingual_preserve_source_style: bool
    about_page: bool


def assemble_outputs(
    store: StoreT,
    *,
    input_path: str,
    progress: ProgressFn | None,
    out_format: str,
    out_path: str | None,
    pdf_engine: str,
    options: PublishingOptions,
    renderer: Renderer,
    bilingual_path: Callable[[str], str],
    stage_call: StageCall,
) -> list[str]:
    """Render the configured mono/bilingual variants from the supplied store."""
    if progress:
        progress(0, 0, "回填译文…")

    # Keep the historical safety default: disabling both switches still emits
    # one monolingual artifact.  Output order is part of the public result.
    do_mono = options.mono
    do_bilingual = options.bilingual
    if not do_mono and not do_bilingual:
        do_mono = True

    outputs: list[str] = []
    if do_mono:
        outputs.append(
            stage_call(
                "assemble",
                renderer,
                store,
                input_path,
                out_path=out_path,
                out_format=out_format,
                bilingual=False,
                about_page=options.about_page,
                pdf_engine=pdf_engine,
            )
        )
    if do_bilingual:
        bilingual_out_path = bilingual_path(out_path) if out_path else None
        outputs.append(
            stage_call(
                "assemble",
                renderer,
                store,
                input_path,
                out_path=bilingual_out_path,
                out_format=out_format,
                bilingual=True,
                order=options.bilingual_order,
                preserve_source_style=options.bilingual_preserve_source_style,
                about_page=options.about_page,
                pdf_engine=pdf_engine,
            )
        )
    return outputs


__all__ = ["PublishingOptions", "assemble_outputs"]
