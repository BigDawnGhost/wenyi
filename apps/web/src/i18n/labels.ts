import { createTranslator, type Locale, type MessageKey } from "./catalog";

type Translator = ReturnType<typeof createTranslator>;

// These names are API identifiers. Translate their presentation, never their submitted values.
export const defaultWorkflowTemplate = "标准翻译";
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

export function languageName(code: string, fallback: string, locale: Locale) {
  try {
    return (
      new Intl.DisplayNames([locale], { type: "language" }).of(code) || fallback
    );
  } catch {
    return fallback;
  }
}
