import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
for (const locale of ["en", "zh-CN"]) {
  test(`permanent project deletion requires confirmation (${locale})`, async ({ page }) => {
    await page.addInitScript((locale) => {
      localStorage.setItem("wenyi.locale", locale);
      // WKWebView can leave browser-native confirmation unavailable.
      window.confirm = () => { throw new Error("Native confirm is unavailable"); };
    }, locale);
    await fakeApi(page, {}, apiOrigin);
    let deleted = false;
    let deleteRequests = 0;
    await page.route("**/projects", async route => {
      if (route.request().method() !== "GET") return route.fallback();
      await route.fulfill({ json: deleted ? [] : [{ ...project, status: "prepared" }] });
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
    const confirmation = page.getByRole("dialog", {
      name: chinese ? "删除项目" : "Delete project", exact: true,
    });
    await button.click();
    await expect(confirmation).toBeVisible();
    await expect(confirmation.getByText(expected, { exact: true })).toBeVisible();
    const cancel = confirmation.getByRole("button", {
      name: chinese ? "取消" : "Cancel", exact: true,
    });
    await expect(cancel).toBeFocused();
    expect(deleteRequests).toBe(0);
    await cancel.click();
    await expect(confirmation).toHaveCount(0);
    await expect(button).toBeFocused();
    await button.click();
    await confirmation.getByRole("button", {
      name: chinese ? "删除项目" : "Delete project", exact: true,
    }).click();
    await expect(button).toHaveCount(0);
    expect(deleteRequests).toBe(1);
    await expect(page.getByText(chinese ? "还没有项目。" : "No projects yet.", { exact: true })).toBeVisible();
  });
  test(`failed deletion stays open for retry and blocks duplicate requests (${locale})`, async ({ page }) => {
    await page.addInitScript((locale) => localStorage.setItem("wenyi.locale", locale), locale);
    await fakeApi(page, {}, apiOrigin);
    const other = { ...project, id: "book-other", name: "Keep this project" };
    let deleted = false;
    let deleteRequests = 0;
    let releaseFailure!: () => void;
    const failure = new Promise<void>((resolve) => { releaseFailure = resolve; });
    await page.route("**/projects", async route => {
      if (route.request().method() !== "GET") return route.fallback();
      await route.fulfill({ json: deleted ? [other] : [{ ...project, status: "prepared" }, other] });
    });
    await page.route(`**/projects/${pid}`, async route => {
      if (route.request().method() !== "DELETE") return route.fallback();
      deleteRequests++;
      if (deleteRequests === 1) {
        await failure;
        await route.fulfill({ status: 409, json: { detail: "Project is busy" } });
      } else {
        deleted = true;
        await route.fulfill({ json: { message: "deleted" } });
      }
    });
    await page.goto("/");
    const chinese = locale === "zh-CN";
    await page.getByRole("button", {
      name: chinese ? `删除项目 ${project.name}` : `Delete project ${project.name}`, exact: true,
    }).click();
    const confirmation = page.getByRole("dialog", {
      name: chinese ? "删除项目" : "Delete project", exact: true,
    });
    const confirm = confirmation.getByRole("button", {
      name: chinese ? "删除项目" : "Delete project", exact: true,
    });
    await confirm.click();
    await expect(confirm).toBeDisabled();
    // A delete that is already running cannot be recalled, but the dialog is
    // only a view of it, so dismissing stays available and the label stops
    // promising a cancel it cannot perform.
    // The footer action is the one under test; the header also has a Close
    // control with the same accessible name.
    const dismiss = confirmation.getByRole("button", {
      name: chinese ? "关闭" : "Close", exact: true,
    }).last();
    await expect(dismiss).toBeEnabled();
    // Escape must actually close the dialog now, and must not send a second
    // request for a delete that is still running.
    await page.keyboard.press("Escape");
    await expect(confirmation).toHaveCount(0);
    expect(deleteRequests).toBe(1);
    // The delete is still in flight while the dialog is closed, so only one
    // failure notice may follow and no duplicate request may be sent.
    releaseFailure();
    await expect(page.getByText(chinese ? /删除失败/ : /Could not delete project/)).toBeVisible();
    expect(deleteRequests).toBe(1);
    // A retry reopens the dialog and sends exactly one more request.
    await page.getByRole("button", {
      name: chinese ? `删除项目 ${project.name}` : `Delete project ${project.name}`, exact: true,
    }).click();
    const retry = page.getByRole("dialog", {
      name: chinese ? "删除项目" : "Delete project", exact: true,
    });
    await retry.getByRole("button", {
      name: chinese ? "删除项目" : "Delete project", exact: true,
    }).click();
    await expect(retry).toHaveCount(0);
    expect(deleteRequests).toBe(2);
    await expect(page.getByRole("button", {
      name: chinese ? `删除项目 ${other.name}` : `Delete project ${other.name}`, exact: true,
    })).toBeVisible();
  });

}
}
