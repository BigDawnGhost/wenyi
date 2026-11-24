import { expect, test } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/sandbox/fixtures";

test("Desktop tests an unsaved API key and never writes it to settings or credentials", async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fake-desktop-token" },
    });
  });
  await fakeApi(page, { "/desktop/credentials": {} });
  let requests = 0;
  const writes: string[] = [];
  page.on("request", request => {
    if (request.method() === "PUT") writes.push(new URL(request.url()).pathname);
  });
  await page.route("**/api/settings/models/ping/draft", async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({
      connection: "deepseek", draft_id: "deepseek",
      models: { "deepseek-flash": {} }, api_key: "fake-draft-key",
    });
    requests++;
    await route.fulfill({
      json: [
        {
          profile: "deepseek/deepseek-flash",
          connection: "deepseek",
          provider: "deepseek",
          model: "deepseek-flash",
          endpoint: null,
          operations: [],
          ok: true,
          category: "ok",
          message: "The provider replied.",
          status_code: null,
          latency_ms: 95,
        },
      ],
    });
  });
  await page.goto("/settings/providers");
  const open = page.getByRole("button", { name: "Edit provider deepseek" });
  await expect(open).toBeEnabled();
  await open.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Edit provider deepseek" });
  const key = dialog.getByLabel("API key", { exact: true });
  await key.fill(" fake-draft-key ");
  await dialog.getByRole("button", { name: "Test connection", exact: true }).click();
  const status = dialog.getByRole("region", { name: "Connections", exact: true }).getByRole("status");
  await expect(status).toContainText("All 1 models are reachable.");
  await expect(dialog.getByRole("table")).toContainText("95 ms");
  await expect(key).toHaveValue(" fake-draft-key ");
  expect(requests).toBe(1);
  expect(writes).toEqual([]);
});
