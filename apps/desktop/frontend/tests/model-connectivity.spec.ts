import { expect, test } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/tests/e2e/fixtures";

test("Desktop tests saved model connections next to the credential fields", async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
    });
  });
  await fakeApi(page);
  let requests = 0;
  await page.route("**/api/settings/models/ping", async (route) => {
    requests++;
    await route.fulfill({
      json: [
        {
          profile: "default_model",
          connection: "default",
          provider: "deepseek",
          model: "deepseek-flash",
          endpoint: null,
          operations: ["translation.body"],
          ok: false,
          category: "missing_credential",
          message: "Provider deepseek has no configured credential",
          status_code: null,
          latency_ms: null,
        },
      ],
    });
  });
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Test connection", exact: true }).click();
  const status = page.getByRole("status").filter({ hasText: "unreachable" });
  await expect(status).toContainText("No API key is configured for this connection.");
  await expect(status).toContainText("—");
  expect(requests).toBe(1);
});
