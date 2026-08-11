"""与 CLI、持久化实现和图运行时解耦的应用服务。"""

from .document_sampling import (
    SourceChapterView,
    SourceDocumentView,
    SourceSegmentView,
    sample_document_text,
)
from .source_language import (
    EmptyLanguageSample,
    InvalidLanguageDetection,
    LanguageDetectionError,
    LanguageDetectionUnavailable,
    LanguageJsonCompletion,
    LanguageResolution,
    ModelSourceLanguageDetector,
    SameSourceAndTargetLanguage,
    SourceLanguageDetector,
    normalize_language_candidate,
    resolve_source_language,
)
from .translation_batches import (
    TranslationBatchPlan,
    TranslationSegmentView,
    plan_contiguous_batches,
    plan_resumable_batches,
)

__all__ = [
    "EmptyLanguageSample",
    "InvalidLanguageDetection",
    "LanguageDetectionError",
    "LanguageDetectionUnavailable",
    "LanguageJsonCompletion",
    "LanguageResolution",
    "ModelSourceLanguageDetector",
    "SameSourceAndTargetLanguage",
    "SourceChapterView",
    "SourceDocumentView",
    "SourceLanguageDetector",
    "SourceSegmentView",
    "TranslationBatchPlan",
    "TranslationSegmentView",
    "plan_contiguous_batches",
    "plan_resumable_batches",
    "normalize_language_candidate",
    "resolve_source_language",
    "sample_document_text",
]
