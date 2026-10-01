"""Writer adapters consume resolved capabilities rather than choose language branches."""

from types import MappingProxyType
from typing import Literal

from ..i18n.policy.models import (
    ExportOptions,
    PolicyContext,
    PolicyPlan,
    content_hash,
)
from ..i18n.policy.resolver import resolve_policy
from .export_view import AssembleStore, ExportViewStore
from .writer_common import _ch_title, _manifest_target_lang

WRITER_OPERATIONS = MappingProxyType(
    {
        "export.style": ("docx.chinese_font",),
        "export.source_markup": ("markup.japanese_ruby",),
        "export.metadata": ("export.language_metadata",),
    }
)


def export_options(store: AssembleStore, format: str) -> ExportOptions:
    if isinstance(store, ExportViewStore):
        return store.language_policy.export
    manifest = store.load_manifest()
    return resolve_policy(
        PolicyContext(
            manifest.get("source_lang", "auto"),
            _manifest_target_lang(manifest),
            phase="export",
            format=format,
        )
    ).export


def export_plan(
    store: AssembleStore,
    format: str,
    *,
    pdf_engine: str = "weasyprint",
    punctuation_normalize: bool = True,
    bilingual: bool = False,
    order: Literal["target_first", "source_first"] = "target_first",
    preserve_source_style: bool = False,
    about_page: bool = True,
) -> PolicyPlan:
    """Compile against the complete consistent snapshot and the actual selected format."""
    manifest = store.load_manifest()
    chapters = []
    for row in manifest["chapters"]:
        chapter = store.load_chapter(row["index"])
        chapters.append(
            {
                "index": chapter.index,
                "title": _ch_title(row),
                "href": chapter.href,
                "template": chapter.template,
                "meta": {
                    key: value
                    for key, value in chapter.meta.items()
                    if not key.startswith("source_digest")
                },
                "segments": [
                    segment.model_dump(mode="json", exclude={"target_before_polish"})
                    for segment in chapter.segments
                ],
            }
        )
    identity = content_hash(
        {
            key: manifest.get(key)
            for key in ("source_sha256", "source_lang", "target_lang", "fmt", "title", "meta")
        }
        | {"chapters": chapters}
    )
    meta = manifest.get("meta", {})
    backend = (
        "babeldoc"
        if format == "pdf" and (meta.get("pdf_export") == "babeldoc" or meta.get("babeldoc"))
        else pdf_engine
        if format == "pdf"
        else "native"
    )
    return resolve_policy(
        PolicyContext(
            manifest.get("source_lang", "auto"),
            _manifest_target_lang(manifest),
            phase="export",
            format=format,
            backend=backend,
            punctuation_normalize=punctuation_normalize,
            source_identity=identity,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
            about_page=about_page,
        ),
    )
