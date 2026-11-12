import { translate as tr } from "../../i18n";

/** Localized labels for every term type the extraction schema can emit. */
export const termTypes = (): Record<string, string> => ({
  person: tr("glossary.person"),
  place: tr("glossary.place"),
  organization: tr("glossary.organization"),
  term: tr("common.terms"),
  technique: tr("glossary.technique"),
  item: tr("glossary.item"),
  setting: tr("glossary.setting"),
  appellation: tr("glossary.appellation"),
  honorific: tr("glossary.honorific"),
  speech: tr("glossary.speechHabit"),
  fixed_expression: tr("glossary.fixedExpression"),
});

/** Terms per request; the page and the import conflict list share one size. */
export const PAGE_SIZE = 100;
