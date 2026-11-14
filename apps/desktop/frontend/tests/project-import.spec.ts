import { expect, test } from "@playwright/test";
import { fakeApi, project } from "../../../../packages/ui/sandbox/fixtures";

const origin = "http://127.0.0.1:19481";

// The folder picker is native, so these fixtures answer with a path directly.
// What is exercised here is everything after it: the request, the refusal text
// and the unavailable card.
async function desktop(page: import("@playwright/test").Page, chosen: string | null) {
  await page.addInitScript(({ origin, chosen }) => {
    window.__WENYI_DESKTOP__ = { apiBase: origin, token: "fixture-token" };
    window.__TAURI_INTERNALS__ = {
      invoke: <T,>(command: string) =>
        Promise.resolve(
          (command === "native_library_choose" ? chosen : null) as T,
        ),
    };
  }, { origin, chosen });
}

test("the Chinese project folder placeholder uses the product name", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await desktop(page, null);
  await fakeApi(page, {}, origin);
  await page.goto("/projects/new");
  const dialog = page.getByRole("dialog", { name: "创建项目", exact: true });
  await expect(dialog.getByLabel("项目文件夹", { exact: true }))
    .toHaveAttribute("placeholder", "文译的默认文件夹");
});

test("the library offers an import entry that adopts the chosen folder", async ({ page }) => {
  await desktop(page, "D:/Downloads/book-1");
  await fakeApi(page, {}, origin);
  let importedPath: string | undefined;
  await page.route("**/desktop/projects/import", async route => {
    importedPath = (route.request().postDataJSON() as { path: string }).path;
    await route.fulfill({
      status: 201,
      json: { ...project, id: "book-9", name: "Imported Book", status: "prepared" },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Import existing project", exact: true }).click();
  await expect(page).toHaveURL("/projects/book-9");
  expect(importedPath).toBe("D:/Downloads/book-1");
});

test("a refused import shows the reason the engine gave", async ({ page }) => {
  await desktop(page, "D:/Downloads/not-a-project");
  await fakeApi(page, {}, origin);
  await page.route("**/desktop/projects/import", route =>
    route.fulfill({
      status: 422,
      json: {
        code: "project_import_failed",
        message: "This folder is not a project: it has no state.sqlite3.",
      },
    }));
  await page.goto("/");
  await page.getByRole("button", { name: "Import existing project", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("it has no state.sqlite3");
  await expect(page).toHaveURL("/");
});

test("a dismissed picker asks the engine for nothing", async ({ page }) => {
  await desktop(page, null);
  await fakeApi(page, {}, origin);
  let asked = 0;
  page.on("request", request => {
    if (request.url().endsWith("/desktop/projects/import")) asked++;
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Import existing project", exact: true }).click();
  await expect(page.getByRole("button", { name: "Import existing project", exact: true }))
    .toBeEnabled();
  expect(asked).toBe(0);
});

test("an empty library never requests legacy startup recovery", async ({ page }) => {
  await desktop(page, null);
  await fakeApi(page, { "/projects": [] }, origin);
  const recoveryRequests: string[] = [];
  page.on("request", request => {
    if (request.url().includes("/desktop/recovery/")) recoveryRequests.push(request.url());
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "My projects", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Import existing project", exact: true }))
    .toBeEnabled();
  await page.reload();
  await expect(page.getByRole("heading", { name: "My projects", exact: true })).toBeVisible();
  expect(recoveryRequests).toEqual([]);
});

test("a project whose folder is missing is shown without an open link", async ({ page }) => {
  await desktop(page, null);
  await fakeApi(page, {}, origin);
  await page.route("**/projects", route =>
    route.fulfill({ json: [{ ...project, name: "Moved Away", available: false }] }));
  await page.goto("/");
  await expect(page.getByText("The folder for this project is not available", { exact: true }))
    .toBeVisible();
  await expect(page.getByRole("link", { name: `Open project Moved Away`, exact: true }))
    .toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete project Moved Away", exact: true }))
    .toBeEnabled();
});
