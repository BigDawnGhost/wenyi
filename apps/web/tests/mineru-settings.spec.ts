import { expect, test } from "@playwright/test";
import { fakeApi } from "../../../packages/ui/sandbox/fixtures";

test("MinerU is an informational deployment setting, including Chinese desktop", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page);
  const nativeRequests: string[] = [];
  page.on("request", request => {
    if (request.url().includes("/desktop/")) nativeRequests.push(request.url());
  });
  await page.goto("/settings/mineru");
  await expect(page).toHaveURL("/settings/pdf");
  await expect(page.getByRole("heading", { name: "PDF", exact: true, level: 1 })).toBeVisible();
  await expect(page.getByTestId("babeldoc-plugin")).toHaveCount(0);
  const card = page.getByTestId("mineru-settings");
  await expect(page.getByRole("heading", { name: "MinerU PDF 解析", exact: true })).toBeVisible();
  await expect(card).toContainText("MINERU_API_KEY");
  await expect(card).toContainText("服务器");
  await expect(card.locator("input")).toHaveCount(0);
  await expect(card.getByRole("button")).toHaveCount(0);
  expect(nativeRequests).toEqual([]);
  await page.goto("/settings/plugins");
  await expect(page).toHaveURL("/settings/pdf");
  await expect(card).toBeVisible();
});
