import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

test("Web keeps environment-variable-only settings", async ({ page }) => {
  // A global left by an extension cannot select a native adapter in this build.
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://invalid.local", token: "must-not-use" },
    __WENYI_DESKTOP_STATUS__: "error",
  }));
  await fakeApi(page);
  await page.goto("/settings/providers");
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  await expect(page.getByLabel("API key environment variable", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Credential source", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("API key", { exact: true })).toHaveCount(0);
});

test("Web history retains browser download", async ({ page }) => {
  await fakeApi(page, {
    [`/projects/${pid}/exports`]: [
      { id: 7, format: "docx", status: "done", size: 123, options: {}, created_at: null },
    ],
  });
  await page.route(`**/projects/${pid}/exports/7/download`, route =>
    route.fulfill({ body: "fixture", contentType: "application/octet-stream" }));
  await page.goto(`/projects/${pid}/export`);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download", exact: true }).click();
  await download;
  await expect(page.getByRole("button", { name: "Save as", exact: true })).toHaveCount(0);
});
