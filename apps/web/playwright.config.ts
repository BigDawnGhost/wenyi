import { defineConfig, devices, type Project } from "@playwright/test";

// The measurement project owns a single worker and depends on the gate project,
// so it only belongs in the nightly run: including it in a pull request would
// serialise the whole suite behind a timing benchmark. PLAYWRIGHT_PERF=1 opts in.
const perfEnabled = process.env.PLAYWRIGHT_PERF === "1";

// PLAYWRIGHT_PROJECTS opts in to extra browser projects by name (comma
// separated). `web-webkit` proxies the Safari/WKWebView/WebKitGTK engine family
// that the Chromium project cannot cover; its browser is installed separately
// with `playwright install webkit`, so it stays out of the default local run.
const enabledProjects = (process.env.PLAYWRIGHT_PROJECTS ?? "")
  .split(",")
  .map((name) => name.trim())
  .filter(Boolean);

// The executable override is Chromium-specific and must not leak into WebKit.
const chromiumUse = {
  launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : {},
};

const gateProjects: Project[] = [
  { name: "web", testIgnore: "**/proofreading-performance.spec.ts", use: chromiumUse },
  ...(enabledProjects.includes("web-webkit")
    ? [
        {
          name: "web-webkit",
          testIgnore: "**/proofreading-performance.spec.ts",
          use: { ...devices["Desktop Safari"] },
        },
      ]
    : []),
];

const perfProjects: Project[] = [
  {
    name: "performance",
    testMatch: "**/proofreading-performance.spec.ts",
    // Measure without competing browser workloads, including other stress tests.
    dependencies: ["web"],
    fullyParallel: false,
    workers: 1,
    use: chromiumUse,
  },
];

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  // A four-core runner derives two workers; CI raises this to three because a
  // retried pass is already reported as flaky instead of passing silently.
  workers: Number(process.env.PLAYWRIGHT_WORKERS) || undefined,
  // Retry once in CI only: a retried pass is reported as flaky, never as green.
  retries: process.env.CI ? 1 : 0,
  projects: perfEnabled ? [...gateProjects, ...perfProjects] : gateProjects,
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
  },
  webServer: {
    command: "./node_modules/.bin/vite --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
  },
});
