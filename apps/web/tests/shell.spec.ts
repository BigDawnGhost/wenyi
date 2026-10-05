import { expect, test } from "@playwright/test";
import { fakeApi } from "./fixtures";

test("navigating between routes keeps the sidebar shell as the same node", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto("/");
  await page.locator("#sidebar-navigation").waitFor();
  await page.evaluate(() => {
    (window as unknown as { __shellNav?: Element | null }).__shellNav =
      document.querySelector("#sidebar-navigation");
  });
  await page.locator('a[href="/settings"]').click();
  await expect(page).toHaveURL("/settings");
  await expect(page.locator("#sidebar-navigation")).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __shellNav?: Element | null }).__shellNav ===
        document.querySelector("#sidebar-navigation"),
    ),
  ).toBe(true);
});

test("unknown paths redirect to the dashboard", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/no-such-page");
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("heading", { name: "My projects" })).toBeVisible();
});
