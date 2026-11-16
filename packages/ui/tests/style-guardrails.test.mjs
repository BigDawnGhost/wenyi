// Style guardrails for the shared frontend (thesis 02, phase 0).
//
// Report-only by default: violations are printed as warnings so the existing
// backlog keeps passing while the token migration is in progress. Set
// STYLE_GUARDRAILS=strict to fail on any violation - that is the phase-4 end
// state once the backlog has been cleaned up.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const HERE = import.meta.dirname;
const ROOTS = [
  join(HERE, "..", "src"),
  join(HERE, "..", "..", "..", "apps", "web", "src"),
  join(HERE, "..", "..", "..", "apps", "desktop", "frontend", "src"),
];

const RULES = [
  {
    id: "template-classname",
    pattern: /className=\{`/
,
    hint: "compose class names with cn() instead of template strings",
  },
  {
    id: "arbitrary-px",
    pattern: /\[[0-9]+px\]/,
    hint: "extend the shared token scale instead of writing arbitrary [Npx] values",
  },
  {
    id: "raw-palette",
    pattern: /\b(?:red|emerald|green|blue|amber|yellow|orange|purple|violet|pink|rose|teal|cyan|sky|indigo|lime|fuchsia|slate|gray|zinc|neutral|stone)-(?:50|100|200|300|400|500|600|700|800|900|950)\b/,
    hint: "use semantic tokens (hsl(var(--...))) instead of raw palette colors",
  },
  {
    id: "state-layout-shift",
    pattern: /(?:hover|focus|active|focus-visible|group-hover):(min-w|min-h|max-w|max-h|gap-x|gap-y|gap|p[xytrbl]?|m[xytrbl]?|w|h)-[^\s"'`}]+/,
    hint: "hover/focus/active must not change layout properties; reserve the space instead",
  },
  {
    id: "transition-all",
    pattern: /\btransition-all\b/,
    hint: "transition a whitelist (transform/opacity/box-shadow/background-color/color) instead of everything",
  },
  {
    id: "inline-layout-style",
    pattern: /style=\{\{[^}]*(?:width|height|margin|padding|gap)\s*:/,
    hint: "layout values belong in tokens/utilities, not inline styles",
  },
];

function collectFiles(dir, out) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) collectFiles(full, out);
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

function scan() {
  const files = ROOTS.flatMap((root) => collectFiles(root, []));
  const violations = [];
  for (const file of files) {
    const lines = readFileSync(file, "utf8").split("\n");
    for (let index = 0; index < lines.length; index += 1) {
      for (const rule of RULES) {
        if (rule.pattern.test(lines[index])) {
          violations.push({ rule: rule.id, file, line: index + 1, text: lines[index].trim().slice(0, 120) });
        }
      }
    }
  }
  return { files: files.length, violations };
}

function format(violations) {
  const byRule = new Map();
  for (const v of violations) byRule.set(v.rule, (byRule.get(v.rule) ?? 0) + 1);
  const head = [...byRule.entries()].map(([rule, count]) => rule + "=" + count).join(", ");
  const sample = violations.slice(0, 40).map((v) => "  " + v.rule + " | " + v.file + ":" + v.line + " | " + v.text);
  return ["style guardrails: " + violations.length + " violation(s) [" + head + "]", ...sample].join("\n");
}

test("style guardrails inventory", () => {
  const { files, violations } = scan();
  assert.ok(files > 80, "expected to scan the shared frontend sources, found " + files);
  if (process.env.STYLE_GUARDRAILS === "strict") {
    assert.equal(violations.length, 0, format(violations));
    return;
  }
  // Phase-0 posture: surface the backlog without blocking development.
  if (violations.length > 0) console.warn(format(violations));
});
