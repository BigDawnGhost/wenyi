import { expect, test } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/sandbox/fixtures";
import { registerDesktopSetup } from "./shared-setup";

registerDesktopSetup();

test("settings entry remains fully visible in a compact desktop work area", async ({ page }) => {
  await page.setViewportSize({ width: 1072, height: 566 });
  await fakeApi(page, { "/projects": [] });
  await page.goto("/");

  const settings = page.getByRole("link", { name: "Settings", exact: true });
  await expect(settings).toBeVisible();
  const bounds = await settings.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(566);
});
