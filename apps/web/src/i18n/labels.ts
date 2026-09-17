import { createTranslator, type Locale, type MessageKey } from "./catalog";

type Translator = ReturnType<typeof createTranslator>;

// These names are API identifiers. Translate their presentation, never their submitted values.
const templates: Record<string, [MessageKey, MessageKey]> = {
  标准翻译: ["workflow.standard", "workflow.standardDescription"],
  快速出稿: ["workflow.quick", "workflow.quickDescription"],
};

export function workflowTemplateLabel(
  name: string,
  description: string,
  t: Translator,
) {
  const keys = templates[name];
  return keys ? `${t(keys[0])} — ${t(keys[1])}` : `${name} — ${description}`;
}

const stages: Record<string, MessageKey> = {
  parse: "workflow.parse",
  prepare: "workflow.prepare",
  language_detection: "workflow.languageDetection",
  style_analysis: "workflow.styleAnalysis",
  book_understanding: "settings.bookUnderstanding",
  translation: "workflow.translate",
  batch_translate: "workflow.translate",
  polish: "settings.polishing",
  annotation_alignment: "settings.paragraphAnnotationAlignment",
  term_extract: "workflow.terms",
  review: "common.wholeBookReview",
  review_autofix: "data.autofix",
  report: "workflow.report",
  assemble: "workflow.export",
  export: "workflow.export",
  srt: "workflow.subtitles",
};

export function workflowStageLabel(
  id: string,
  fallback: string,
  t: Translator,
) {
  return stages[id] ? t(stages[id]) : fallback;
}

const operations: Record<string, MessageKey> = {
  "language.detect": "workflow.languageDetection",
  "analysis.style": "workflow.styleAnalysis",
  "synopsis.chapter": "accounting.chapterSynopsis",
  "synopsis.book": "accounting.bookSynopsis",
  "translation.body": "workflow.translate",
  "translation.title": "accounting.translateTitles",
  "polish.body": "settings.polishing",
  "glossary.extract": "workflow.terms",
  "glossary.align_history": "accounting.alignTerms",
  "annotation.align": "settings.paragraphAnnotationAlignment",
  "review.scan": "accounting.reviewScan",
  "review.verify": "accounting.reviewVerify",
  "review.arbitrate": "accounting.reviewArbitrate",
  "review.fix": "accounting.reviewFix",
  "autofix.verify": "accounting.autofixVerify",
  "autofix.fix": "accounting.autofixFix",
  "srt.translate": "workflow.subtitles",
  translate: "workflowPanel.bookTranslation",
  workflow: "accounting.workflow",
  unknown: "accounting.unknownAttribution",
};

/** Keep unfamiliar operation IDs visible until a translation is registered. */
export function operationLabel(id: string, t: Translator) {
  return operations[id]
    ? t(operations[id])
    : workflowStageLabel(id, id || "—", t);
}

export function languageName(code: string, fallback: string, locale: Locale) {
  try {
    return (
      new Intl.DisplayNames([locale], { type: "language" }).of(code) || fallback
    );
  } catch {
    return fallback;
  }
}
