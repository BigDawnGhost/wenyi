// Offline production-build comparison from apps/desktop/frontend:
// node tests/measure-desktop.mjs /path/to/web/dist /path/to/desktop/frontend/dist
// All project responses are disposable fixtures; no local service is contacted.
import { chromium } from "@playwright/test";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, extname } from "node:path";
import assert from "node:assert/strict";

const [baselineDir, currentDir] = process.argv.slice(2);
if (!baselineDir || !currentDir) throw new Error("Provide baseline and current dist directories.");
const project = {
  id: "fixture", name: "Performance fixture", title: "Fixture", fmt: "epub",
  source_lang: "en", target_lang: "zh", initialized: true, status: "translating",
  chapter_count: 1, total_word_count: 12, done_chapters: 0,
};
const data = {
  "/projects": [project],
  "/projects/fixture": project,
  "/projects/fixture/chapters": [{
    index: 0, title: "Fixture chapter", status: "translating", word_count: 12,
    target_word_count: 0, review_issue_count: 0, review_status: "pending",
  }],
  "/projects/fixture/workflow": {
    source: "snapshot", kind: "translation", status: "running", run_id: "fixture-run",
    stages: [], progress: null,
  },
  "/projects/fixture/stats": { usage: { totals: { calls: 0 } }, timing: { runs: [], total_seconds: 0 } },
  "/projects/fixture/report": { summary: {} },
};

async function serve(directory) {
  const root = resolve(directory);
  const server = createServer(async (request, response) => {
    try {
      const path = new URL(request.url, "http://fixture").pathname;
      const file = path.startsWith("/assets/") ? resolve(root, "." + path) : resolve(root, "index.html");
      if (!file.startsWith(root + "/")) throw new Error("Invalid path");
      const types = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".png": "image/png" };
      response.setHeader("Content-Type", types[extname(file)] || "application/octet-stream");
      response.end(await readFile(file));
    } catch {
      response.writeHead(404).end();
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return { server, origin: `http://127.0.0.1:${server.address().port}` };
}

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
});
async function measure(directory, desktop) {
  const { server, origin } = await serve(directory);
  const context = await browser.newContext();
  try {
    if (desktop) await context.addInitScript(() => {
      window.__WENYI_DESKTOP_PENDING__ = true;
      window.__WENYI_DESKTOP__ = { apiBase: "http://127.0.0.1:19481", token: "fixture-only" };
    });
    const page = await context.newPage();
    await page.routeWebSocket("**/ws/**", () => {});
    await page.route(desktop ? "http://127.0.0.1:19481/**" : "**/api/**", route => {
      const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
      return route.fulfill({ json: data[path] || {} });
    });
    const js = [];
    page.on("response", response => {
      if (new URL(response.url()).pathname.endsWith(".js"))
        js.push(response.body().then(body => body.length));
    });
    await page.goto(origin);
    await page.getByRole("link", { name: /Performance fixture/ }).waitFor();
    await page.waitForLoadState("networkidle");
    const firstLoadJsBytes = (await Promise.all(js)).reduce((a, b) => a + b, 0);
    await page.goto(`${origin}/projects/fixture`);
    await page.getByRole("heading", { name: "Performance fixture" }).waitFor();
    await page.getByText("Progress connection: Live").waitFor();
    // Exclude mount, snapshot reconciliation and the 500ms event coalescing window.
    await page.waitForTimeout(1500);
    let fetches = 0;
    const count = request => { if (request.resourceType() === "fetch") fetches++; };
    page.on("request", count);
    await page.waitForTimeout(10_000);
    page.off("request", count);
    return { firstLoadJsBytes, steadyProgressFetches10s: fetches };
  } finally {
    await context.close();
    await new Promise(resolve => server.close(resolve));
  }
}

try {
  const baseline = await measure(baselineDir, false);
  const desktop = await measure(currentDir, true);
  console.log(JSON.stringify({ baseline, desktop }, null, 2));
  // These are different platform entries, not evidence of a performance improvement.
  // Desktop legitimately includes bootstrap and native capabilities absent from Web.
  assert(desktop.firstLoadJsBytes > 0 && baseline.firstLoadJsBytes > 0);
  assert(desktop.steadyProgressFetches10s < baseline.steadyProgressFetches10s);
} finally {
  await browser.close();
}
