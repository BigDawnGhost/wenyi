import { expect, test } from "@playwright/test";
import type { ModelPingResult } from "@wenyi/ui/api/types";
import { fakeApi } from "../../../packages/ui/sandbox/fixtures";
import { jsonModeChecks } from "../../../packages/ui/sandbox/json-mode-checks";

jsonModeChecks();

test("global settings test saved model connections and explain failures", async ({ page }) => {
  const requests: unknown[] = [];
  let results: ModelPingResult[] = [
    {
      profile: "default_model",
      connection: "default",
      provider: "deepseek",
      model: "deepseek-flash",
      endpoint: "https://api.deepseek.com",
      operations: ["translation.body"],
      ok: false,
      category: "authentication_failed",
      message: "Provider authentication failed. Check the API key.",
      status_code: 401,
      latency_ms: 210,
      json_mode: null,
      json_category: null,
      json_status_code: null,
    },
  ];
  await fakeApi(page);
  await page.route("**/api/settings/models/ping", async (route) => {
    requests.push(route.request().postDataJSON());
    await route.fulfill({ json: results });
  });
  await page.goto("/settings/providers");
  const test = page.getByRole("button", { name: "Test connection", exact: true });
  await expect(test).toBeEnabled();
  await test.click();
  const status = page.getByRole("status").filter({ hasText: "unreachable" });
  await expect(status).toContainText("1 of 1 models are unreachable.");
  await expect(status).toContainText("Authentication failed. Check the API key. (HTTP 401)");
  await expect(status).toContainText("210 ms");
  expect(requests).toEqual([{ detect_json: true }]);

  results = [{ ...results[0], ok: true, category: "ok", status_code: null, latency_ms: 95 }];
  await test.click();
  await expect(page.getByRole("status").filter({ hasText: "reachable" })).toContainText(
    "All 1 models are reachable.",
  );
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();

  // Unsaved edits are not what the backend would test.
  await page.getByLabel("API base URL", { exact: true }).fill("https://unsaved.invalid/v1");
  await expect(test).toBeDisabled();
  await expect(page.getByText("Save the configuration before testing the connection.")).toBeVisible();
  expect(requests).toHaveLength(2);
});

test("a slow probe counts elapsed seconds and clears the count with its results", async ({ page }) => {
  await fakeApi(page);
  let release!: () => void;
  const answered = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/settings/models/ping", async (route) => {
    await answered;
    await route.fulfill({ json: [] });
  });
  await page.goto("/settings/providers");
  await page.clock.install({ time: new Date("2025-01-01T00:00:00Z") });
  await page.getByRole("button", { name: "Test connection", exact: true }).click();
  const elapsed = page.getByText(/elapsed/);
  await expect(elapsed).toHaveText("0 s elapsed");
  await page.clock.runFor(3000);
  await expect(elapsed).toHaveText("3 s elapsed");
  release();
  await expect(page.getByText("No models are configured.")).toBeVisible();
  await expect(elapsed).toHaveCount(0);
});
