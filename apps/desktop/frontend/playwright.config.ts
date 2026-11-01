import { defineConfig, type Project } from "@playwright/test";

// The measurement project owns a single worker and depends on the gate project,
// so it only belongs in the nightly run: including it in a pull request would
// serialise the whole suite behind a timing benchmark. PLAYWRIGHT_PERF=1 opts in.
const perfEnabled = process.env.PLAYWRIGHT_PERF === "1";

const gateProjects: Project[] = [
  { name: "desktop", testIgnore: "**/proofreading-performance.spec.ts" },
];

const perfProjects: Project[] = [
  {
    name: "desktop-performance",
    testMatch: "**/proofreading-performance.spec.ts",
    dependencies: ["desktop"],
    fullyParallel: false,
    workers: 1,
  },
];

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  // Retry once in CI only: a retried pass is reported as flaky, never as green.
  retries: process.env.CI ? 1 : 0,
  projects: perfEnabled ? [...gateProjects, ...perfProjects] : gateProjects,
  use: {
    baseURL: "http://127.0.0.1:4174",
    headless: true,
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
  },
  webServer: {
    command: "./node_modules/.bin/vite --host 127.0.0.1 --port 4174",
    url: "http://127.0.0.1:4174",
    reuseExistingServer: !process.env.CI,
  },
});
