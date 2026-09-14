"""Immutable EPUB theme resources and semantic output identity."""

from trans_novel.assemble.epub.rendering.theme.contracts import (
    ElementPath,
    InlineChange,
    LayoutBinding,
    MarkerChange,
    NoteAttributeChange,
    NoteChange,
    NoteNamespaceChange,
    NotePathMapping,
    NoteTextChange,
    ResourceThemePlan,
    ResourceThemeScope,
    SourcePair,
    ThemeBundle,
    ThemeError,
    ThemePlan,
)
from trans_novel.assemble.epub.rendering.theme.loading import (
    resolve_theme,
    semantic_output_digest,
)

__all__ = [
    "ElementPath",
    "InlineChange",
    "LayoutBinding",
    "MarkerChange",
    "NoteAttributeChange",
    "NoteChange",
    "NoteNamespaceChange",
    "NotePathMapping",
    "NoteTextChange",
    "ResourceThemePlan",
    "ResourceThemeScope",
    "SourcePair",
    "ThemeBundle",
    "ThemeError",
    "ThemePlan",
    "resolve_theme",
    "semantic_output_digest",
]
