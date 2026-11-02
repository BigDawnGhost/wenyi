import { expect, test } from "@playwright/test";
import { fakeApi, project } from "../../../packages/ui/sandbox/fixtures";

// The Web host has no folder picker, so the entry point must be absent rather
// than present and broken. Both locales are checked because the label is copy.
for (const chinese of [false, true]) {
  test(`${chinese ? "Chinese" : "English"} Web dashboard offers no folder import`, async ({ page }) => {
    await page.addInitScript(
      (locale) => localStorage.setItem("wenyi.locale", locale),
      chinese ? "zh-CN" : "en",
    );
    await fakeApi(page, { "/projects": [project] });
    await page.goto("/");
    await expect(page.getByRole("heading", {
      name: chinese ? "我的项目" : "My projects", exact: true,
    })).toBeVisible();
    await expect(page.getByRole("button", {
      name: chinese ? "导入已有项目" : "Import existing project", exact: true,
    })).toHaveCount(0);
  });
}

test("a project whose folder is missing is greyed out with no way in", async ({ page }) => {
  await fakeApi(page, { "/projects": [{ ...project, name: "Moved Away", available: false }] });
  await page.goto("/");
  await expect(
    page.getByText("The folder for this project is not available", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open project Moved Away", exact: true }),
  ).toHaveCount(0);
  // Removal stays available: a folder that is gone still has to be tidied up.
  await expect(
    page.getByRole("button", { name: "Delete project Moved Away", exact: true }),
  ).toBeEnabled();
});
