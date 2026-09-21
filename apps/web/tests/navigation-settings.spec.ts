import { test, expect } from "@playwright/test";
import { fakeApi, pid, project, configuration, effective } from "./fixtures";

for (const mobile of [false, true]) {
  test(`project navigation stays flat on ${mobile ? "mobile in Chinese" : "desktop"}`, async ({
    page,
  }, testInfo) => {
    if (mobile) {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.addInitScript(() =>
        localStorage.setItem("wenyi.locale", "zh-CN"),
      );
    }
    await fakeApi(page);
    await page.goto(`/projects/${pid}/proofreading`);
    const nav = page.getByRole("navigation", {
      name: mobile ? "项目导航" : "Project navigation",
    });
    await expect(nav.getByRole("link")).toHaveCount(9);
    await expect(nav.getByRole("link")).toHaveText(
      mobile
        ? [
            "翻译总览",
            "人工校阅",
            "全书审校",
            "术语表",
            "风格 & 概要",
            "目录与标题",
            "导出",
            "项目配置",
            "事件日志",
          ]
        : [
            "Translation overview",
            "Manual proofreading",
            "Whole-book review",
            "Glossary",
            "Style & synopsis",
            "Contents & titles",
            "Export",
            "Project settings",
            "Event log",
          ],
    );
    await expect(nav.locator("details, summary")).toHaveCount(0);
    await expect(
      nav.getByRole("link", {
        name: mobile ? "翻译总览" : "Translation overview",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      nav.getByRole("link", {
        name: mobile ? "人工校阅" : "Manual proofreading",
        exact: true,
      }),
    ).toHaveAttribute("aria-current", "page");
    await expect(
      nav.getByRole("link", {
        name: mobile ? "事件日志" : "Event log",
        exact: true,
      }),
    ).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("project-navigation.png"),
      fullPage: true,
    });
    await page.goto(`/projects/${pid}/settings`);
    await expect(
      page.getByRole("heading", {
        name: mobile ? "项目配置" : "Project settings",
        exact: true,
      }),
    ).toBeVisible();
    await expect(nav.locator('a[href$="/settings"]')).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(nav.locator('a[href$="/settings"]')).toBeVisible();
    await page.reload();
    await expect(nav.locator('a[href$="/settings"]')).toBeVisible();
    await expect(
      page.getByLabel(mobile ? "界面语言" : "Interface language"),
    ).toHaveCount(0);
  });
}

test("subtitle navigation stays flat and omits book-only pages", async ({
  page,
}) => {
  await fakeApi(page, { [`/projects/${pid}`]: { ...project, fmt: "srt" } });
  await page.goto(`/projects/${pid}/subtitles`);
  const nav = page.getByRole("navigation", { name: "Project navigation" });
  await expect(nav.getByRole("link")).toHaveCount(5);
  await expect(
    nav.getByRole("link", { name: /Glossary|Style|review/i }),
  ).toHaveCount(0);
});

test("advanced settings retain invalid drafts and reveal them after validation", async ({
  page,
}) => {
  const settings = {
    ...effective,
    llm: {
      tiers: { strong: "main", cheap: "main", fast: "main" },
      routes: { "translation.body": { model: "main" } },
    },
  };
  await fakeApi(page, {
    [`/projects/${pid}/config`]: {
      ...configuration,
      registered_models: {
        main: { provider: "default", model: "deepseek-flash" },
      },
      effective: settings,
      yaml: JSON.stringify(settings),
    },
  });
  let submitted: typeof settings | undefined;
  await page.route(`**/api/projects/${pid}/config/validate`, async (route) => {
    submitted = JSON.parse(route.request().postDataJSON().yaml);
    return route.fulfill({
      status: 422,
      json: {
        detail: "segment.max_tokens_per_batch must be greater than zero",
      },
    });
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(page.getByLabel("Quality tier")).toBeVisible();
  await expect(
    page.getByLabel("Apply autofixes to the saved translation after review", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByLabel("API provider", { exact: true }),
  ).not.toBeVisible();
  await expect(page.getByLabel("Tokens per batch")).not.toBeVisible();
  const performance = page
    .locator("summary")
    .filter({ hasText: "Segmentation and performance" });
  await performance.click();
  await page.getByLabel("Tokens per batch").fill("0");
  await performance.click();
  await page
    .getByRole("button", { name: "Validate configuration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("max_tokens_per_batch");
  await expect(page.getByLabel("Tokens per batch")).toBeVisible();
  await expect(page.getByLabel("Tokens per batch")).toHaveValue("0");
  expect(submitted?.segment.max_tokens_per_batch).toBe(0);
  expect(submitted?.llm.routes).toEqual(settings.llm.routes);
  await expect(page.getByLabel("PDF parser")).toHaveCount(0);
});
