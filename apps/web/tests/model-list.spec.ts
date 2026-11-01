import { expect, test } from "@playwright/test";
import { fakeApi } from "./fixtures";

test("fetching models for a connection fills a searchable model-name list", async ({ page }) => {
  await fakeApi(page);
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/settings/models/list", async (route) => {
    requests.push(route.request().postDataJSON());
    await route.fulfill({
      json: { ok: true, models: ["deepseek-flash", "deepseek-pro", "other-model"], category: "ok", message: "" },
    });
  });
  await page.goto("/settings/providers");
  const name = page.getByRole("combobox", { name: "Model name", exact: true });
  await expect(page.getByRole("listbox")).toHaveCount(0);
  // The draft (unsaved) base URL is used for the request.
  await page.getByLabel("API base URL", { exact: true }).fill("https://draft.invalid/v1");
  await page.getByRole("button", { name: "Fetch models", exact: true }).click();
  const list = page.getByRole("listbox", { name: "Model name" });
  await expect(list.getByRole("option")).toHaveCount(3);
  await expect(page.getByText("3 models available", { exact: true })).toBeVisible();
  expect(requests).toEqual([
    {
      connection: "default",
      provider: expect.objectContaining({ kind: "deepseek", base_url: "https://draft.invalid/v1" }),
      api_key: null,
    },
  ]);
  await name.fill("pro");
  await expect(list.getByRole("option")).toHaveText(["deepseek-pro"]);
  await name.press("ArrowDown");
  await name.press("Enter");
  await expect(name).toHaveValue("deepseek-pro");
  await expect(list).toHaveCount(0);
  // Free text is still accepted.
  await name.fill("custom-name");
  await name.press("Escape");
  await expect(name).toHaveValue("custom-name");
  // Editing the connection makes the list stale.
  await page.getByLabel("API base URL", { exact: true }).fill("https://changed.invalid/v1");
  await expect(page.getByText("3 models available", { exact: true })).toHaveCount(0);
});

test("listing failures explain the cause in Chinese", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page);
  await page.route("**/api/settings/models/list", (route) =>
    route.fulfill({
      json: { ok: false, models: [], category: "authentication_failed", message: "x", status_code: 401 },
    }));
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "获取模型列表", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "认证失败" })).toBeVisible();
  await expect(page.getByRole("listbox")).toHaveCount(0);
});

for (const outcome of ["success", "failure", "http error"] as const) {
  test(`a late listing ${outcome} is ignored after editing the connection`, async ({ page }) => {
    await fakeApi(page);
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    let requests = 0;
    await page.route("**/api/settings/models/list", async route => {
      requests++;
      if (requests === 1) {
        await gate;
        if (outcome === "http error")
          return route.fulfill({ status: 503, json: { detail: "Listing unavailable" } });
        return route.fulfill({ json: outcome === "success"
          ? { ok: true, models: ["old-server-model"], category: "ok", message: "" }
          : { ok: false, models: [], category: "authentication_failed", message: "", status_code: 401 },
        });
      }
      return route.fulfill({ json: {
        ok: true, models: ["new-server-model"], category: "ok", message: "",
      } });
    });
    try {
      await page.goto("/settings/providers");
      const fetch = page.getByRole("button", { name: "Fetch models", exact: true });
      const submitted = page.waitForRequest("**/api/settings/models/list");
      await fetch.click();
      await submitted;
      await page.getByLabel("API base URL", { exact: true }).fill("https://new-server.invalid/v1");
      release();
      await expect(fetch).toBeEnabled();
      await expect(page.getByRole("listbox")).toHaveCount(0);
      await expect(page.getByRole("status").filter({
        hasText: /models available|Authentication failed|Could not fetch/,
      })).toHaveCount(0);
      await fetch.click();
      await expect(page.getByRole("option")).toHaveText(["new-server-model"]);
      await page.getByRole("option", { name: "new-server-model", exact: true }).click();
      await expect(page.getByRole("combobox", { name: "Model name", exact: true }))
        .toHaveValue("new-server-model");
    } finally {
      release();
    }
  });
}
