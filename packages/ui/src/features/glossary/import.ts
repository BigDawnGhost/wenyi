import type { Term } from "../../api/types";
import type { MessageKey } from "../../i18n/catalog";

type Translate = (key: MessageKey, values?: Record<string, unknown>) => string;

/**
 * Parse the CSV export format back into terms.
 *
 * The reader is hand-written rather than split-based because a quoted cell may
 * contain commas, doubled quotes and newlines, and the header order is not
 * fixed. An unterminated quote is a hard error: silently returning partial rows
 * would import a truncated glossary.
 *
 * The translator is passed in so the parser stays free of the i18n runtime and
 * can be unit-tested outside a browser.
 */
export function parseCsv(text: string, translate: Translate): Partial<Term>[] {
  const rows: string[][] = [];
  let row: string[] = [],
    value = "",
    quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') {
        value += '"';
        i++;
      } else quoted = !quoted;
    } else if (!quoted && char === ",") {
      row.push(value);
      value = "";
    } else if (!quoted && char === "\n") {
      row.push(value.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      value = "";
    } else value += char;
  }
  if (quoted) throw new Error(translate("glossary.unclosedQuoteInCsv"));
  if (value || row.length) {
    row.push(value.replace(/\r$/, ""));
    rows.push(row);
  }
  const headers = (rows.shift() || []).map((h) =>
    h.trim().replace(/^\uFEFF/, ""),
  );
  return rows
    .filter((values) => values.some(Boolean))
    .map((vals) => {
      const row: Record<string, unknown> = {};
      headers.forEach((h, i) => {
        row[h] = vals[i] ?? "";
      });
      return {
        source: String(row.source ?? ""),
        target: String(row.target ?? ""),
        reading: String(row.reading ?? ""),
        type: String(row.type ?? "term"),
        gender: String(row.gender ?? ""),
        note: String(row.note ?? ""),
        aliases: row.aliases
          ? String(row.aliases).split("|").filter(Boolean)
          : [],
      };
    });
}
