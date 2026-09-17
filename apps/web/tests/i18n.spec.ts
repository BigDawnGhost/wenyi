import { test, expect } from "@playwright/test";
import { locales, createTranslator } from "../src/i18n/catalog";
import { statusLabel, statusTone } from "../src/i18n/status";
import { fakeApi, pid } from "./fixtures";

test("locale catalogs have matching keys and interpolation parameters", () => {
  const english = locales.en.messages;
  for (const language of Object.values(locales)) {
    expect(Object.keys(language.messages).sort()).toEqual(
      Object.keys(english).sort(),
    );
    for (const [key, message] of Object.entries(english)) {
      const localized = language.messages[key as keyof typeof english];
      const parameters = (value: string) =>
        [...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
      expect(parameters(localized), key).toEqual(parameters(message));
      expect(localized.trim(), key).not.toBe("");
    }
  }
  expect(
    createTranslator("en")("glossary.selectedCount", { count: 1234 }),
  ).toBe("Selected: 1,234");
  expect(
    createTranslator("zh-CN")("glossary.selectedCount", { count: 2 }),
  ).toBe("已选 2 项");
});

test.describe("browser language preference", () => {
  test.use({ locale: "zh-CN" });
  test("defaults to English and persists an explicit Chinese selection", async ({
    page,
  }) => {
    await fakeApi(page);
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "My projects" }),
    ).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page).toHaveTitle("Wenyi — AI translation");
    await page.getByRole("link", { name: "Settings", exact: true }).click();
    await expect(page.getByLabel("Interface language")).toHaveValue("en");
    await page.getByLabel("Interface language").selectOption("zh-CN");
    await expect(
      page.getByRole("heading", { name: "设置", exact: true }),
    ).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
    await expect(page).toHaveTitle("文译 — AI 翻译");
    await page.reload();
    await expect(page.getByLabel("界面语言")).toHaveValue("zh-CN");
    await page.getByRole("link", { name: "项目列表", exact: true }).click();
    await expect(page.getByRole("heading", { name: "我的项目" })).toBeVisible();
    await expect(page.getByText("原文", { exact: true })).toBeVisible();
  });
});

test("interface language is available only in global settings without changing project configuration", async ({
  page,
}) => {
  await fakeApi(page);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") writes.push(request.url());
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.getByRole("button", { name: "Save configuration", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Interface language")).toHaveCount(0);
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByLabel("Interface language").selectOption("zh-CN");
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.getByRole("button", { name: "保存配置", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("界面语言")).toHaveCount(0);
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await page.getByLabel("界面语言").selectOption("en");
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.getByRole("button", { name: "Save configuration", exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect(page.getByText("原文第一段", { exact: false })).toBeVisible();
  await expect(
    page.getByText("Original translation", { exact: true }),
  ).toBeVisible();
});

test("language preference synchronizes between tabs and unknown locales fall back to English", async ({
  page,
  context,
}) => {
  await fakeApi(page);
  await page.addInitScript(() =>
    localStorage.setItem("wenyi.locale", "unsupported-language"),
  );
  await page.goto("/settings");
  await expect(page.getByLabel("Interface language")).toHaveValue("en");
  const other = await context.newPage();
  await fakeApi(other);
  await other.goto("/settings");
  await page.getByLabel("Interface language").selectOption("zh-CN");
  await expect(other.getByLabel("界面语言")).toHaveValue("zh-CN");
  await other.getByLabel("界面语言").selectOption("en");
  await expect(page.getByLabel("Interface language")).toHaveValue("en");
  await other.close();
});

test("known workflow labels and language names are localized without changing API identifiers", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto("/projects/new");
  await expect(
    page.getByLabel("Target language").locator('option[value="en"]'),
  ).toHaveText("English (en)");
  await expect(page.getByLabel("Translation workflow")).toHaveCount(0);
  await page.goto("/settings");
  await expect(page.getByLabel("Default workflow template")).toHaveValue(
    "标准翻译",
  );
  await expect(
    page
      .getByLabel("Default workflow template")
      .locator('option[value="标准翻译"]'),
  ).toContainText("Standard translation");
  await page.goto(`/projects/${pid}`);
  await page.getByText("Workflow details", { exact: true }).click();
  await expect(
    page.getByText("Translate chapters in batches", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("分批翻译章节", { exact: true })).toHaveCount(0);
});

for (const locale of ["en", "zh-CN"] as const) {
  test(`localizes glossary, style, event and export pages in ${locale}`, async ({
    page,
    context,
  }) => {
    const events = [
      {
        id: 1,
        type: "run_initialized",
        payload: { chapters: 2 },
        created_at: "2026-09-17T08:00:00Z",
      },
      {
        id: 2,
        type: "analysis_saved",
        payload: {},
        created_at: "2026-09-17T08:00:00Z",
      },
    ];
    await fakeApi(page, {
      [`/projects/${pid}/glossary/terms`]: [
        {
          source: "原文角色",
          target: "Translated character",
          type: "person",
          aliases: [],
        },
      ],
      [`/projects/${pid}/glossary/conflicts`]: [],
      [`/projects/${pid}/analysis`]: {
        analysis: {
          genre: "原文风格",
          characters: [],
          style_guide: "User style guidance",
        },
        chapter_digests: [],
      },
      [`/projects/${pid}/events`]: events,
    });
    await page.goto(`/projects/${pid}/glossary`);
    const settings = await context.newPage();
    await fakeApi(settings);
    await settings.goto("/settings");
    await settings.getByLabel("Interface language").selectOption(locale);
    await settings.close();
    const chinese = locale === "zh-CN";
    await expect(
      page.getByRole("heading", {
        name: chinese ? "术语表" : "Glossary",
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByText("原文角色", { exact: true })).toBeVisible();
    await expect(
      page.getByText(chinese ? "人物" : "Person", { exact: true }).last(),
    ).toBeVisible();
    await page
      .getByRole("button", {
        name: chinese ? "添加术语" : "Add term",
        exact: true,
      })
      .click();
    await expect(
      page.getByText(chinese ? "源词 *" : "Source term *", { exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: chinese ? "关闭" : "Close", exact: true })
      .click();
    await page.goto(`/projects/${pid}/style`);
    await expect(
      page.getByRole("heading", {
        name: chinese ? "风格 & 概要" : "Style & synopsis",
      }),
    ).toBeVisible();
    await expect(page.getByText("原文风格", { exact: true })).toBeVisible();
    await page.goto(`/projects/${pid}/events`);
    await expect(
      page.getByText(
        chinese ? "项目初始化：2 章" : "Project initialized — chapters: 2",
        { exact: true },
      ),
    ).toBeVisible();
    const summaries = [
      chinese ? "风格分析完成" : "Style analysis completed",
      chinese ? "项目初始化：2 章" : "Project initialized — chapters: 2",
    ];
    await expect(page.locator("main summary")).toHaveText(summaries);
    events.push({
      id: 3,
      type: "book_synopsis_saved",
      payload: {},
      created_at: "2026-09-17T08:01:00Z",
    });
    await page.reload();
    await expect(page.locator("main summary")).toHaveText([
      chinese ? "生成全书概览" : "Book synopsis generated",
      ...summaries,
    ]);
    await page.goto(`/projects/${pid}/export`);
    await expect(
      page.getByRole("button", {
        name: chinese ? "生成导出文件" : "Generate export",
      }),
    ).toBeVisible();
  });
}

test("language settings remain accessible from mobile navigation", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fakeApi(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByLabel("Interface language").selectOption("zh-CN");
  await expect(
    page.getByRole("heading", { name: "设置", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("界面语言")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("mobile-language-settings.png"),
    fullPage: true,
  });
});

test("status aliases share labels and tones while chapter pending retains its meaning", () => {
  const en = createTranslator("en"),
    zh = createTranslator("zh-CN");
  expect(statusLabel("done", en)).toBe(statusLabel("completed", en));
  expect(statusLabel("done", zh)).toBe("已完成");
  expect(statusTone("failed")).toBe(statusTone("error"));
  expect(statusLabel("pending", en)).toBe("Pending");
  expect(statusLabel("pending", zh, "chapter")).toBe("待翻译");
  expect(statusLabel("custom_unrecognized", en)).toBe("Unknown status");
});
