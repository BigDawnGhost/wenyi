import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

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

test("project navigation defaults to the first project on global routes", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto("/");
  const projectNav = page.getByRole("navigation", {
    name: "Project navigation",
  });
  await expect(projectNav).toBeVisible();
  await expect(projectNav).toContainText("Test Book");
  await projectNav
    .getByRole("link", { name: "Translation overview", exact: true })
    .click();
  await expect(page).toHaveURL(`/projects/${pid}`);
});

test("project navigation follows the last opened project across global routes", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto(`/projects/${pid}`);
  const projectNav = page.getByRole("navigation", {
    name: "Project navigation",
  });
  await expect(projectNav).toBeVisible();
  await page
    .getByRole("navigation", { name: "Global navigation" })
    .getByRole("link", { name: "Projects", exact: true })
    .click();
  await expect(page).toHaveURL("/");
  await expect(projectNav).toBeVisible();
  await page
    .locator('[data-slot="sidebar.footer"] a[href="/settings"]')
    .click();
  await expect(page).toHaveURL("/settings");
  await expect(projectNav).toBeVisible();
  await page
    .locator('[data-slot="sidebar.action"] a[href="/projects/new"]')
    .click();
  await expect(page).toHaveURL("/projects/new");
  await expect(projectNav).toBeVisible();
  // The preference survives a reload.
  await page.reload();
  await expect(projectNav).toBeVisible();
});

test("sidebar follows the most recently opened project after switching", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto("/");
  await page
    .getByRole("link", { name: "Open project 雾谷试译", exact: true })
    .click();
  await expect(page).toHaveURL("/projects/book-2");
  const projectNav = page.getByRole("navigation", {
    name: "Project navigation",
  });
  await expect(projectNav).toContainText("雾谷试译");
  await page
    .getByRole("navigation", { name: "Global navigation" })
    .getByRole("link", { name: "Projects", exact: true })
    .click();
  await expect(page).toHaveURL("/");
  await expect(projectNav).toContainText("雾谷试译");
});
