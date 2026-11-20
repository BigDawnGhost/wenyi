import type { Term } from "./types";

// Match the byte limit on the shared glossary import route.
export const MAX_GLOSSARY_IMPORT_MIB = 5;
export const MAX_GLOSSARY_IMPORT_BYTES = MAX_GLOSSARY_IMPORT_MIB * 1024 * 1024;

/** Return the complete request body, or null if its UTF-8 encoding exceeds the limit. */
export function glossaryImportBody(terms: Partial<Term>[]): string | null {
  const body = JSON.stringify({
    terms: terms.map(term => ({
      source: term.source || "",
      target: term.target || "",
      reading: term.reading || "",
      type: term.type || "term",
      gender: term.gender || "",
      aliases: term.aliases || [],
      note: term.note || "",
    })),
  });
  return new TextEncoder().encode(body).length <= MAX_GLOSSARY_IMPORT_BYTES ? body : null;
}
