import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const uiSrc = resolve(root, "packages/ui/src");

function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? files(resolve(directory, entry.name))
      : [resolve(directory, entry.name)],
  );
}

const frontendRoots = [
  uiSrc,
  resolve(root, "apps/web/src"),
  resolve(root, "apps/desktop/frontend/src"),
];
const sources = frontendRoots
  .flatMap((directory) => files(directory))
  .filter((file) => /\.(ts|tsx)$/.test(file));

// Suspense may only live in these gates: the route content gate and the
// widget-level lazy wrapper. Nothing may wrap the shell with Suspense.
const suspenseAllowed = new Set([
  join("packages", "ui", "src", "routes", "RouteGate.tsx"),
  join("packages", "ui", "src", "routes", "LazyBoundary.tsx"),
]);

test("Suspense exists only inside the route and lazy gates", () => {
  assert(sources.length > 50, "The rule must scan the real frontend source tree");
  const offenders = sources.filter(
    (file) =>
      readFileSync(file, "utf8").includes("<Suspense") &&
      !suspenseAllowed.has(relative(root, file)),
  );
  assert.deepEqual(
    offenders.map((file) => relative(root, file)),
    [],
    "Wrap the widget with LazyBoundary (routes/LazyBoundary.tsx) or move the gate into routes/RouteGate.tsx",
  );
  for (const shell of [
    join("packages", "ui", "src", "App.tsx"),
    join("packages", "ui", "src", "components", "layout", "AppLayout.tsx"),
  ]) {
    const code = readFileSync(resolve(root, shell), "utf8");
    assert(!code.includes("<Suspense"), `${shell} must not gate rendering directly`);
    assert(!code.includes("RouteBoundary"), `${shell} must use routes/AppErrorBoundary instead`);
  }
});

test("every lazy chunk is paired with a local gate or an explicit warm-up", () => {
  const exempt = new Set([join("packages", "ui", "src", "routes", "manifest.tsx")]);
  for (const file of sources) {
    const rel = relative(root, file);
    if (exempt.has(rel)) continue;
    const code = readFileSync(file, "utf8");
    if (!code.includes("lazy(")) continue;
    assert(
      /<Suspense|LazyBoundary|void load/.test(code),
      `${rel} uses lazy() without a LazyBoundary/Suspense or an explicit "void load..." warm-up`,
    );
  }
});

test("route manifest is complete and consistent", () => {
  const manifest = readFileSync(join(uiSrc, "routes", "manifest.tsx"), "utf8");

  const paths = [...manifest.matchAll(/path:\s*"([^"]+)"/g)].map((m) => m[1]);
  assert(paths.length >= 14, `expected the full route table, found ${paths.length}`);
  assert.equal(new Set(paths).size, paths.length, "route paths must be unique");

  const loadersBlock = manifest.match(/pageLoaders[\s\S]*?=\s*\[([\s\S]*?)\]/);
  assert(loadersBlock, "pageLoaders must exist for warm-up and prefetch");
  const loaderCount = (loadersBlock[1].match(/load[A-Z]\w*/g) || []).length;
  assert.equal(loaderCount, 13, "one loader per code-split page");

  const navLines = manifest
    .split("\n")
    .filter((line) => /group:\s*"(global|project)"/.test(line) && line.includes('to: "'));
  assert.equal(navLines.length, 13, "three global + ten project nav entries");
  const seen = new Set();
  for (const line of navLines) {
    const to = line.match(/to:\s*"([^"]+)"/)?.[1];
    const order = line.match(/order:\s*(\d+)/)?.[1];
    const audience = line.match(/audience:\s*"(all|book|srt)"/)?.[1];
    const loader = line.match(/loader:\s*(load[A-Z]\w*)/)?.[1];
    const group = line.match(/group:\s*"(global|project)"/)?.[1];
    assert(to && order && audience && loader, `incomplete nav entry: ${line.trim()}`);
    assert(paths.includes(to), `nav target ${to} has no route`);
    const key = `${group}:${to}`;
    assert(!seen.has(key), `duplicate nav entry ${key}`);
    seen.add(key);
  }

  const app = readFileSync(join(uiSrc, "App.tsx"), "utf8");
  assert(app.includes("routeEntries.map"), "App must render routes from the manifest");
  assert(app.includes("pageLoaders.map"), "App must warm chunks from the manifest");
  assert(!/path="\/[^"]/.test(app), "App must not declare route paths outside the manifest");
});
