import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
for (const locale of ["en", "zh-CN"]) {
  test(`permanent project deletion requires confirmation (${locale})`, async ({ page }) => {
    await page.addInitScript((locale) => {
      localStorage.setItem("wenyi.locale", locale);
    }, locale);
    await fakeApi(page, {}, apiOrigin);
    let deleted = false;
    let deleteRequests = 0;
    await page.route("**/projects", async route => {
      if (route.request().method() !== "GET") return route.fallback();
      await route.fulfill({ json: deleted ? [] : [project] });
    });
    await page.route(`**/projects/${pid}`, async route => {
      if (route.request().method() !== "DELETE") return route.fallback();
      deleteRequests++;
      deleted = true;
      await route.fulfill({ json: { message: "deleted" } });
    });
    await page.goto("/");
    const chinese = locale === "zh-CN";
    const button = page.getByRole("button", {
      name: chinese ? `删除项目 ${project.name}` : `Delete project ${project.name}`,
      exact: true,
    });
    await expect(button).toBeVisible();
    const expected = chinese
      ? `确认永久删除项目“${project.name}”及其全部原文、译文、历史记录、缓存和项目导出文件？此操作无法撤销。已另存到其他位置的文件不会被删除。`
      : `Permanently delete project “${project.name}” and all its original files, translations, history, caches, and project exports? This cannot be undone. Copies saved elsewhere will not be deleted.`;
    page.once("dialog", async dialog => {
      expect(dialog.message()).toBe(expected);
      await dialog.dismiss();
    });
    await button.click();
    expect(deleteRequests).toBe(0);
    await expect(button).toBeVisible();
    page.once("dialog", async dialog => {
      expect(dialog.message()).toBe(expected);
      await dialog.accept();
    });
    await button.click();
    await expect(button).toHaveCount(0);
    expect(deleteRequests).toBe(1);
    await expect(page.getByText(chinese ? "还没有项目。" : "No projects yet.", { exact: true })).toBeVisible();
  });
}
}
