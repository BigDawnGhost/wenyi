import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(resolve(directory, entry.name)) : [resolve(directory, entry.name)],
  );
}
const nativeCode = /__TAURI|__WENYI_DESKTOP|isDesktop|native_drop_|native_export_|\/desktop\/credentials|System credential store|系统凭据库/;
// One lazy chunk per route in packages/ui/src/App.tsx; keep in sync with the route manifest.
const routeChunks = [
  "Dashboard",
  "CreateProject",
  "ProgressPage",
  "GlossaryPage",
  "StylePage",
  "ReviewPage",
  "ProofreadingPage",
  "ExportPage",
  "EventsPage",
  "ContentsPage",
  "InterfaceSettingsPage",
  "SettingsPage",
  "SubtitlesPage",
];

test("shared UI cannot import hosts or own platform transports/storage", () => {
  const sources = files(resolve(root, "packages/ui/src")).filter((file) => /\.(ts|tsx)$/.test(file));
  assert(sources.length > 50, "The boundary check must scan the real shared source tree");
  for (const file of sources) {
    const code = readFileSync(file, "utf8");
    assert(!nativeCode.test(code), `${file} contains a native implementation`);
    assert(!/\b(?:localStorage|sessionStorage)\b|\bfetch\s*\(|new WebSocket\s*\(/.test(code),
      `${file} bypasses injected platform services`);
    for (const match of code.matchAll(/(?:from\s*|import\s*\(\s*)["']([^"']+)["']/g)) {
      const specifier = match[1];
      const target = specifier.startsWith(".") ? resolve(dirname(file), specifier) : specifier;
      assert(!/[/\\]apps[/\\]|@wenyi\/(?:web|desktop)/.test(target), `${file} imports host ${specifier}`);
    }
  }
});

test("production Web output contains no native bootstrap, IPC or vault UI", () => {
  const artifacts = files(resolve(root, "apps/web/dist"));
  assert(artifacts.some((file) => /wenyi-emblem-.*\.png$/.test(file)), "Web must emit the shared brand asset");
  const outputs = artifacts.filter((file) => /\.(js|html)$/.test(file));
  assert(outputs.length > 10, "Build Web before running bundle assertions");
  for (const file of outputs)
    assert(!nativeCode.test(readFileSync(file, "utf8")), `Native implementation found in ${file}`);
  for (const route of routeChunks)
    assert(outputs.some((file) => basename(file).includes(`${route}-`)), `${route} must remain a lazy chunk`);
});

test("Desktop independently includes the native adapters and keeps routes lazy", () => {
  const artifacts = files(resolve(root, "apps/desktop/frontend/dist"));
  assert(artifacts.some((file) => /wenyi-emblem-.*\.png$/.test(file)), "Desktop must emit the shared brand asset");
  const outputs = artifacts.filter((file) => /\.js$/.test(file));
  const code = outputs.map((file) => readFileSync(file, "utf8")).join("\n");
  for (const marker of ["__WENYI_DESKTOP", "native_drop_upload", "native_export_save", "/desktop/credentials"])
    assert(code.includes(marker), `Desktop is missing ${marker}`);
  for (const route of routeChunks)
    assert(outputs.some((file) => basename(file).includes(`${route}-`)), `${route} must remain a lazy chunk`);
});
