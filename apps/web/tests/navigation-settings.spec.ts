import { test, expect } from "@playwright/test";
import { fakeApi, pid, project, configuration, effective } from "./fixtures";

for (const mobile of [false, true]) {
  test(`project tools are accessible and deep links expand on ${mobile ? "mobile in Chinese" : "desktop"}`, async ({
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
    await expect(nav.getByRole("link")).toHaveCount(4);
    await expect(
      nav.getByRole("link", {
        name: mobile ? "人工校阅" : "Manual proofreading",
        exact: true,
      }),
    ).toHaveAttribute("aria-current", "page");
    const tools = nav.locator("summary");
    await tools.focus();
    await tools.press("Enter");
    await expect(nav.getByRole("link")).toHaveCount(8);
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

test("subtitle navigation omits book-only pages even inside project tools", async ({
  page,
}) => {
  await fakeApi(page, { [`/projects/${pid}`]: { ...project, fmt: "srt" } });
  await page.goto(`/projects/${pid}/subtitles`);
  const nav = page.getByRole("navigation", { name: "Project navigation" });
  await expect(nav.getByRole("link")).toHaveCount(3);
  await nav.locator("summary").click();
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
      preset: "deepseek",
      providers: { default: { kind: "deepseek" } },
      models: { main: { provider: "default", model: "deepseek-flash" } },
      tiers: { strong: "main", cheap: "main", fast: "main" },
      routes: { "translation.body": { model: "main" } },
    },
  };
  await fakeApi(page, {
    [`/projects/${pid}/config`]: {
      ...configuration,
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
    page.getByLabel("Apply autofixes to the saved translation", {
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
