import { expect, test } from "@playwright/test";
import type { ModelPingResult } from "../../../packages/ui/src/lib/api";
import { fakeApi } from "./fixtures";

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
  expect(requests).toEqual([{}]);

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
