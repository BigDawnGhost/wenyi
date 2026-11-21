import { expect, test } from "@playwright/test";
import type { ModelPingResult } from "@wenyi/ui/api/types";
import { fakeApi, globalConfiguration } from "../../../packages/ui/sandbox/fixtures";

test("save provider makes the updated connection testable without another settings save", async ({ page }) => {
  const requests: unknown[] = [];
  let writes = 0;
  let saved = globalConfiguration;
  let results: ModelPingResult[] = [
    {
      profile: "deepseek/deepseek-flash",
      connection: "deepseek",
      provider: "deepseek",
      model: "deepseek-flash",
      endpoint: "https://api.deepseek.com",
      operations: ["translation.body"],
      ok: false,
      category: "authentication_failed",
      message: "Provider authentication failed. Check the API key.",
      status_code: 401,
      latency_ms: 210,
    },
  ];
  await fakeApi(page);
  await page.route("**/api/settings", async route => {
    if (route.request().method() === "PUT") {
      writes++;
      const { yaml } = route.request().postDataJSON();
      saved = { ...saved, yaml, effective: JSON.parse(yaml), revision: saved.revision + 1 };
    }
    await route.fulfill({ json: saved });
  });
  await page.route("**/api/settings/models/ping", async (route) => {
    requests.push(route.request().postDataJSON());
    expect(saved.effective.llm.providers.deepseek).toMatchObject({ base_url: "https://updated.invalid/v1" });
    await route.fulfill({ json: results });
  });
  await page.goto("/settings/providers");
  await expect(page.getByRole("button", { name: "Test connection", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await page.getByLabel("API base URL", { exact: true }).fill("https://updated.invalid/v1");
  const test = page.getByRole("button", { name: "Test connection", exact: true });
  await expect(test).toBeDisabled();
  await expect(page.getByRole("dialog")).toContainText("Click Save provider in this dialog");
  await page.getByRole("button", { name: "Save provider", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes).toBe(1);
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await expect(page.getByLabel("API base URL", { exact: true })).toHaveValue("https://updated.invalid/v1");
  await expect(test).toBeEnabled();
  await test.click();
  const status = page.getByRole("status").filter({ hasText: "unreachable" });
  await expect(status).toContainText("1 of 1 models are unreachable.");
  await expect(status).toContainText("Authentication failed. Check the API key. (HTTP 401)");
  await expect(status).toContainText("210 ms");
  expect(requests).toEqual([{ connection: "deepseek" }]);

  results = [{ ...results[0], ok: true, category: "ok", status_code: null, latency_ms: 95 }];
  await test.click();
  await expect(page.getByRole("status").filter({ hasText: "reachable" })).toContainText(
    "All 1 models are reachable.",
  );
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();

  expect(requests).toHaveLength(2);
  expect(writes).toBe(1);
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
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
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
