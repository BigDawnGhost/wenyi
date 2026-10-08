import { expect, test } from "@playwright/test";
import { fakeApi } from "./fixtures";

test("MinerU is an informational deployment setting, including Chinese mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page);
  const nativeRequests: string[] = [];
  page.on("request", request => {
    if (request.url().includes("/desktop/")) nativeRequests.push(request.url());
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  await expect(card).toContainText("MinerU");
  await expect(card).toContainText("MINERU_API_KEY");
  await expect(card).toContainText("PDF");
  await expect(card).toContainText("服务器");
  await expect(card.locator("input")).toHaveCount(0);
  await expect(card.getByRole("button")).toHaveCount(0);
  expect(nativeRequests).toEqual([]);
});
