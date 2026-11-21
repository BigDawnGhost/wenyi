// parseCsv turns the exported CSV back into terms, so its quoting rules are
// pinned here: the browser suites can only see the happy path.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  glossaryImportBody,
  MAX_GLOSSARY_IMPORT_BYTES,
} from "../src/api/glossary-import.ts";
import { parseCsv } from "../src/features/glossary/import.ts";

const translate = (key) => `localized:${key}`;

const term = (overrides) => ({
  source: "",
  target: "",
  reading: "",
  type: "term",
  gender: "",
  note: "",
  aliases: [],
  ...overrides,
});

test("a header row maps columns by name, not by position", () => {
  const [parsed] = parseCsv("target,source,type\n譯名,原文,person\n", translate);
  assert.deepEqual(parsed, term({ source: "原文", target: "譯名", type: "person" }));
});

test("quoted cells keep commas, newlines and doubled quotes", () => {
  const [parsed] = parseCsv(
    'source,target,note\n"A, B","line1\nline2","say ""hi"""\n',
    translate,
  );
  assert.equal(parsed.source, "A, B");
  assert.equal(parsed.target, "line1\nline2");
  assert.equal(parsed.note, 'say "hi"');
});

test("a UTF-8 BOM on the first header does not become part of the name", () => {
  const [parsed] = parseCsv("\uFEFFsource,target\n原文,譯名\n", translate);
  assert.equal(parsed.source, "原文");
});

test("blank and separator-only lines are dropped instead of becoming empty terms", () => {
  assert.deepEqual(parseCsv("source,target\n\n原文,譯名\n,\n", translate), [
    term({ source: "原文", target: "譯名" }),
  ]);
});

test("aliases split on the pipe separator and drop empties", () => {
  const [parsed] = parseCsv("source,target,aliases\n原文,譯名,a||b\n", translate);
  assert.deepEqual(parsed.aliases, ["a", "b"]);
});

test("an unterminated quote fails instead of importing partial rows", () => {
  assert.throws(
    () => parseCsv('source,target\n"原文,譯名\n', translate),
    /localized:glossary\.unclosedQuoteInCsv/,
  );
});

test("a missing type column falls back to the generic term type", () => {
  const [parsed] = parseCsv("source,target\n原文,譯名\n", translate);
  assert.equal(parsed.type, "term");
});

test("request limits include the UTF-8 bytes, JSON envelope and separators", () => {
  const terms = [{ source: "原文", target: "譯名" }, { source: "a", target: "b" }];
  const remaining = MAX_GLOSSARY_IMPORT_BYTES - Buffer.byteLength(glossaryImportBody(terms));
  terms[0].note = "字".repeat(Math.floor(remaining / 3)) + "x".repeat(remaining % 3);

  assert.equal(Buffer.byteLength(glossaryImportBody(terms)), MAX_GLOSSARY_IMPORT_BYTES);

  terms[0].note += "x";
  assert.ok(terms.every(entry => glossaryImportBody([entry]) !== null));
  assert.equal(glossaryImportBody(terms), null);
});

test("a single entry can exceed the byte limit after JSON escaping", () => {
  const terms = [{ source: "a", target: "b", note: "\\".repeat(MAX_GLOSSARY_IMPORT_BYTES / 2) }];
  assert.equal(glossaryImportBody(terms), null);
});
