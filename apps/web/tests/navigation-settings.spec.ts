import { test, expect } from "@playwright/test";
import { fakeApi, pid, project, configuration, effective } from "../../../packages/ui/sandbox/fixtures";

test("project settings retain creation-time precision without a mode selector", async ({ page }) => {
  await fakeApi(page, {
    [`/projects/${pid}/config`]: {
      ...configuration,
      effective: {
        ...effective,
        pipeline: { ...effective.pipeline, translation_mode: "three_draft_synthesis", polish: true },
      },
    },
  });
  let submitted: Record<string, any> | undefined;
  await page.route(`**/api/projects/${pid}/config`, async (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    submitted = JSON.parse(route.request().postDataJSON().yaml);
    await route.fulfill({
      json: { ...configuration, effective: submitted, yaml: JSON.stringify(submitted) },
    });
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(page.getByLabel("Translation mode", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Polishing", { exact: true })).toBeChecked();
  await expect(page.getByLabel("Polishing", { exact: true })).toBeDisabled();
  await expect(page.getByLabel("Parallel initial drafts (1–3)")).toHaveCount(0);
  await page.getByLabel("Whole-book review", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  await expect(page.getByText("Project settings saved", { exact: true })).toBeVisible();
  expect(submitted?.pipeline.translation_mode).toBe("three_draft_synthesis");
  expect(submitted?.pipeline.polish).toBe(true);
  expect(submitted?.pipeline.review).toBe(false);
  expect(submitted?.pipeline.precision_concurrency).toBeUndefined();
  expect(submitted?.segment.max_tokens_per_batch).toBe(1800);
  await expect(page.getByLabel("Polishing", { exact: true })).toBeDisabled();
});

for (const chinese of [false, true]) {
  test(`project navigation stays flat in ${chinese ? "Chinese" : "English"}`, async ({
    page,
  }, testInfo) => {
    if (chinese) {
      await page.setViewportSize({ width: 1024, height: 844 });
      await page.addInitScript(() =>
        localStorage.setItem("wenyi.locale", "zh-CN"),
      );
    }
    await fakeApi(page);
    await page.goto(`/projects/${pid}/proofreading`);
    const nav = page.getByRole("navigation", {
      name: chinese ? "项目导航" : "Project navigation",
    });
    await expect(nav.getByRole("link")).toHaveCount(9);
    await expect(nav.getByRole("link")).toHaveText(
      chinese
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
        name: chinese ? "翻译总览" : "Translation overview",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      nav.getByRole("link", {
        name: chinese ? "人工校阅" : "Manual proofreading",
        exact: true,
      }),
    ).toHaveAttribute("aria-current", "page");
    await expect(
      nav.getByRole("link", {
        name: chinese ? "事件日志" : "Event log",
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
        name: chinese ? "项目配置" : "Project settings",
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
      page.getByLabel(chinese ? "界面语言" : "Interface language"),
    ).toHaveCount(0);
  });
}

test("project settings retain invalid inline YAML and prevent basic edits from overwriting it", async ({
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
  await expect(page.getByLabel("Tokens per batch")).toHaveCount(0);
  const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true });
  const invalid = JSON.stringify({
    ...settings,
    segment: { ...settings.segment, max_tokens_per_batch: 0 },
  });
  await yaml.fill(invalid);
  await expect(page.getByLabel("Quality tier", { exact: true })).toBeDisabled();
  await expect(page.getByLabel("Polishing", { exact: true })).toBeDisabled();
  await page.getByRole("link", { name: /Advanced YAML has unvalidated changes/ }).click();
  await expect(page).toHaveURL(`/projects/${pid}/settings#advanced-yaml`);
  await expect(yaml).toBeInViewport();
  await expect(yaml).toHaveValue(invalid);
  await page
    .getByRole("button", { name: "Validate configuration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("max_tokens_per_batch");
  await expect(yaml).toHaveValue(invalid);
  expect(submitted?.segment.max_tokens_per_batch).toBe(0);
  expect(submitted?.llm.routes).toEqual(settings.llm.routes);
  await expect(page.getByLabel("PDF parser")).toHaveCount(0);
});
