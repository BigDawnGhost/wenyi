import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  // Retry once in CI only: a retried pass is reported as flaky, never as green.
  retries: process.env.CI ? 1 : 0,
  projects: [
    { name: "desktop", testIgnore: "**/proofreading-performance.spec.ts" },
    {
      name: "desktop-performance",
      testMatch: "**/proofreading-performance.spec.ts",
      dependencies: ["desktop"],
      fullyParallel: false,
      workers: 1,
    },
  ],
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
