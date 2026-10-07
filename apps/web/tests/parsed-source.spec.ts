import { expect, test } from "@playwright/test";
import { chapter, fakeApi, pid, project } from "./fixtures";

for (const { locale, entry } of [
  { locale: "en", entry: "contents" },
  { locale: "zh-CN", entry: "contents" },
  { locale: "en", entry: "proofreading" },
  { locale: "zh-CN", entry: "proofreading" },
]) {
  test(`parsed source remains readable before AI preparation (${locale}, ${entry})`, async ({ page }, info) => {
    test.setTimeout(60000);
    const origin = info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined;
    const api = origin ?? "**/api";
    await page.addInitScript(({ origin, locale }) => {
      localStorage.setItem("wenyi.locale", locale);
      if (origin) Object.assign(window, {
        __WENYI_DESKTOP__: { apiBase: origin, token: "parsed-source-fixture" },
      });
    }, { origin, locale });
    await fakeApi(page, {}, origin);
    const state = { parsed: false, initialized: false, status: "parsing" };
    const writes: string[] = [];
    const unsupported: string[] = [];
    page.on("request", (request) => {
      if (request.method() !== "GET" && request.url().includes("/projects/")) writes.push(request.url());
      if (/\/(history|precision)/.test(request.url())) unsupported.push(request.url());
    });
    await page.route(`${api}/projects/${pid}`, (route) => route.fulfill({
      json: { ...project, initialized: state.initialized, status: state.status },
    }));
    await page.route(`${api}/projects/${pid}/chapters`, (route) => route.fulfill({
      json: state.parsed ? [{ ...chapter, status: "pending", target_word_count: 0 }] : [],
    }));
    await page.route(`${api}/projects/${pid}/review/0`, (route) => route.fulfill({
      json: {
        index: 0, title: chapter.title, title_translated: null, review_issues: [],
        segments: [{ index: 0, kind: "text", source: "Parsed source paragraph.", target: state.initialized ? "Translated paragraph." : null }],
      },
    }));
    const zh = locale === "zh-CN";
    const contents = zh ? "目录与标题" : "Contents & titles";
    const editTitle = zh ? "编辑标题" : "Edit title";
    const open = zh ? "查看原文" : "Read source";
    await page.goto(`/projects/${pid}/${entry}`);
    await expect(page.getByText(zh ? "解析完成后显示章节。" : "Chapters will appear after parsing.")).toBeVisible();
    state.parsed = true;
    state.status = "uploaded";
    await page.waitForResponse(async (response) =>
      response.url().endsWith(`/projects/${pid}`) && (await response.json()).status === "uploaded",
    );
    if (entry === "contents") {
      await expect(page.getByRole("list", { name: contents })).toContainText(chapter.title, { timeout: 10000 });
      await expect(page.getByRole("button", { name: editTitle })).toBeDisabled();
      await page.getByRole("link", { name: open, exact: true }).click();
    } else {
      await page.getByRole("link", { name: new RegExp(chapter.title) }).click();
    }
    await expect(page.getByText("Parsed source paragraph.", { exact: true })).toBeVisible();
    await expect(page.getByText(zh ? /AI 预处理尚未完成/ : /AI preparation is not complete/)).toBeVisible();
    for (const status of ["preparing", "error"]) {
      state.status = status;
      await page.waitForResponse(async (response) =>
        response.url().endsWith(`/projects/${pid}`) && (await response.json()).status === status,
      );
      await expect(page.getByRole("button", { name: zh ? "段落操作" : "Paragraph actions" })).toHaveCount(0);
      await expect(page.getByText("Parsed source paragraph.", { exact: true })).toBeVisible();
    }
    await page.goto(`/projects/${pid}`);
    await expect(page.getByRole("button", { name: zh ? "翻译此章" : "Translate chapter", exact: true })).toBeDisabled();
    await page.getByRole("link", { name: zh ? "查看目录与原文" : "Read contents & source", exact: true }).click();
    await expect(page.getByRole("button", { name: editTitle })).toBeDisabled();
    state.initialized = true;
    state.status = "done";
    await expect(page.getByRole("button", { name: editTitle })).toBeEnabled({ timeout: 10000 });
    await page.getByRole("link", { name: zh ? "跳转人工校阅" : "Open in proofreading" }).click();
    await expect(page.getByText("Translated paragraph.", { exact: true })).toBeVisible({ timeout: 10000 });
    await page.getByRole("button", { name: zh ? "段落操作" : "Paragraph actions" }).click();
    await expect(page.getByRole("menuitem", { name: zh ? "编辑译文" : "Edit translation" })).toBeEnabled();
    expect(writes).toEqual([]);
    expect(unsupported).toEqual([]);
  });
}
