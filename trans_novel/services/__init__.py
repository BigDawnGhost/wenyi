"""与 CLI、持久化实现和图运行时解耦的确定性服务。"""

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
    ModelSourceLanguageDetector,
    SourceLanguageDetector,
    normalize_language_candidate,
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
    "ModelSourceLanguageDetector",
    "SourceChapterView",
    "SourceDocumentView",
    "SourceLanguageDetector",
    "SourceSegmentView",
    "TranslationBatchPlan",
    "TranslationSegmentView",
    "normalize_language_candidate",
    "plan_contiguous_batches",
    "plan_resumable_batches",
    "sample_document_text",
]
