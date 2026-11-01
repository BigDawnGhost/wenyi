import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

test.beforeEach(async ({ page }, info) => {
  if (info.project.name === "desktop") {
    await page.addInitScript(() => Object.assign(window, {
      __WENYI_DESKTOP__: {
        apiBase: "http://127.0.0.1:4174/api",
        token: "settings-fixture-token",
      },
    }));
  }
  await fakeApi(page);
});

for (const chinese of [false, true]) {
  test(`settings replaces project navigation and returns to the entry page in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
    await page.setViewportSize({ width: 1024, height: 768 });
    const origin = `/projects/${pid}/proofreading?view=translation#chapter`;
    await page.goto(origin);
    await page.getByRole("link", { name: chinese ? "设置" : "Settings", exact: true }).click();
    const nav = page.getByRole("navigation", { name: chinese ? "设置导航" : "Settings navigation" });
    await expect(nav).toBeVisible();
    await expect(page.getByRole("navigation", { name: chinese ? "项目导航" : "Project navigation" })).toHaveCount(0);
    await expect(page.getByRole("link", { name: chinese ? "创建项目" : "Create project", exact: true })).toHaveCount(0);
    await expect(nav.getByRole("link", { name: chinese ? "通用" : "General", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(nav.getByRole("link")).toHaveCount(testInfo.project.name === "desktop" ? 9 : 6);
    await expect(page.getByLabel(chinese ? "界面语言" : "Interface language")).toBeVisible();
    await expect(page.getByLabel(chinese ? "润色" : "Polishing", { exact: true })).not.toBeVisible();
    await expect(page.locator('[data-slot="settings.actions"]')).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath("settings-general.png") });
    await nav.getByRole("link", { name: chinese ? "翻译流程" : "Workflow", exact: true }).click();
    await expect(page).toHaveURL("/settings/workflow");
    await nav.getByRole("link", { name: chinese ? "模型与接口" : "Models & connections", exact: true }).click();
    await page.reload();
    await expect(nav.getByRole("link", { name: chinese ? "模型与接口" : "Models & connections", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.getByLabel(chinese ? "模型 ID" : "Model ID", { exact: true })).toBeVisible();
    await expect.poll(() => page.getByRole("main").evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("settings-providers.png") });
    await page.getByRole("button", { name: chinese ? "返回" : "Back", exact: true }).click();
    await expect(page).toHaveURL(origin);
  });
}

test("direct settings links have a safe return destination", async ({ page }) => {
  await page.goto("/settings/providers");
  await expect(page.getByLabel("Model ID", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Default models", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page).toHaveURL("/");
});

test("category history preserves drafts and closes dropdowns", async ({ page }) => {
  await page.goto("/settings/workflow");
  await page.getByLabel("Polishing", { exact: true }).uncheck();
  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await page.getByLabel("Model ID", { exact: true }).fill("unfinished model");
  await page.getByRole("link", { name: "General", exact: true }).click();
  await expect(page).toHaveURL("/settings");
  await expect(page.getByLabel("Interface language", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toBeDisabled();
  await page.getByRole("link", { name: "Apply or discard the ID edits before saving.", exact: true }).click();
  await expect(page).toHaveURL("/settings/providers");
  await expect(page.getByRole("link", { name: "Models & connections", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveValue("unfinished model");
  await page.getByLabel("API provider", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL("/settings");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await page.getByRole("link", { name: "Workflow", exact: true }).click();
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
});

for (const chinese of [false, true]) {
  test(`standalone settings show all controls without folding in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/settings/workflow");
    const tokens = page.getByLabel(chinese ? "每批 Token 预算" : "Tokens per batch", { exact: true });
    await expect(tokens).toBeVisible();
    await expect(page.getByLabel(chinese ? "每段 Token 预算" : "Tokens per paragraph", { exact: true })).toBeVisible();
    await expect(page.getByLabel(chinese ? "审校并发数" : "Review concurrency", { exact: true })).toBeVisible();
    await expect(page.getByLabel(chinese ? "PDF 解析后端" : "PDF parser", { exact: true })).toBeVisible();
    await expect(page.getByRole("main").locator("details, summary")).toHaveCount(0);
    await tokens.fill("1400");
    await page.screenshot({ path: testInfo.outputPath("settings-workflow-expanded.png") });
    await page.getByRole("link", { name: chinese ? "默认模型" : "Default models", exact: true }).click();
    await expect(page.getByLabel(chinese ? "分批翻译章节" : "Translate chapters in batches", { exact: true })).toBeVisible();
    await page.getByRole("link", { name: chinese ? "高级" : "Advanced", exact: true }).click();
    const yaml = page.getByRole("textbox", { name: chinese ? "高级 YAML 配置" : "Advanced YAML configuration", exact: true });
    await expect(yaml).toBeVisible();
    const draft = await yaml.inputValue();
    expect(JSON.parse(draft).segment.max_tokens_per_batch).toBe(1400);
    await yaml.fill(`${draft}\n# unsaved draft`);
    await page.getByRole("link", { name: chinese ? "通用" : "General", exact: true }).click();
    await page.getByRole("link", { name: chinese ? "高级" : "Advanced", exact: true }).click();
    await expect(yaml).toHaveValue(`${draft}\n# unsaved draft`);
  });
}

test("project settings retain their compact disclosures", async ({ page }) => {
  await page.goto(`/projects/${pid}/settings`);
  await expect(page.getByLabel("Tokens per batch", { exact: true })).not.toBeVisible();
  await expect(page.getByLabel("Translate chapters in batches", { exact: true })).not.toBeVisible();
  await expect(page.getByLabel("Advanced YAML configuration", { exact: true })).not.toBeVisible();
  await page.locator("summary").filter({ hasText: "Segmentation and performance" }).click();
  await expect(page.getByLabel("Tokens per batch", { exact: true })).toBeVisible();
});

test("category content uses the full pane without a duplicate title column", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/providers");
  const categories = [
    "Models & connections", "Workflow", "Default models", "PDF parsing", "Advanced", "General",
    ...(testInfo.project.name === "desktop" ? ["BabelDOC plugin", "Diagnostics and feedback", "About"] : []),
  ];
  for (const category of categories) {
    await page.getByRole("link", { name: category, exact: true }).click();
    await expect(page.getByRole("heading", { name: category, level: 1, exact: true })).toBeVisible();
    const section = page.locator('[data-slot="settings.section"]:visible');
    await expect(section).toHaveCount(1);
    await expect(section.locator(":scope > h2")).not.toBeVisible();
    await expect.poll(() => section.evaluate(node => {
      const content = node.querySelector(":scope > div")!.getBoundingClientRect();
      const bounds = node.getBoundingClientRect();
      return Math.abs(content.left - bounds.left) <= 1 && Math.abs(content.width - bounds.width) <= 1;
    })).toBe(true);
    await expect.poll(() => page.getByRole("main").evaluate(node => {
      const container = node.querySelector('[data-slot="page.container"]')!;
      return container.getBoundingClientRect().width === node.clientWidth;
    })).toBe(true);
  }
});

for (const chinese of [false, true]) {
  test(`settings keeps Back at the bottom and uses clear category names in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    const desktop = testInfo.project.name === "desktop";
    await page.addInitScript(({ locale, desktop }) => {
      localStorage.setItem("wenyi.locale", locale);
      if (desktop) Object.assign(window, {
        __TAURI_INTERNALS__: {
          invoke: async (command: string) => command === "desktop_update_status" ? {
            currentVersion: "1.0.0", mode: "automatic", phase: "current",
            downloaded: 0, total: 0,
          } : null,
        },
      });
    }, { locale: chinese ? "zh-CN" : "en", desktop });
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/");
    await page.getByRole("link", { name: chinese ? "设置" : "Settings", exact: true }).click();
    const nav = page.getByRole("navigation", { name: chinese ? "设置导航" : "Settings navigation" });
    const back = page.getByRole("button", { name: chinese ? "返回" : "Back", exact: true });
    const bottomGap = () => back.evaluate(button => {
      const sidebar = button.closest("aside")!.getBoundingClientRect();
      return sidebar.bottom - button.getBoundingClientRect().bottom;
    });
    await expect(nav).toBeVisible();
    await expect(page.locator('[data-slot="sidebar.footer"]')).toHaveCSS("border-top-width", "0px");
    await expect.poll(bottomGap).toBeLessThanOrEqual(16);
    await expect(nav.getByRole("link")).toHaveText(chinese
      ? ["通用", "模型与接口", "默认模型", "翻译流程", "PDF 解析",
        ...(desktop ? ["BabelDOC 插件"] : []), "高级",
        ...(desktop ? ["诊断与反馈", "关于"] : [])]
      : ["General", "Models & connections", "Default models", "Workflow", "PDF parsing",
        ...(desktop ? ["BabelDOC plugin"] : []), "Advanced",
        ...(desktop ? ["Diagnostics and feedback", "About"] : [])]);
    if (desktop) {
      await nav.getByRole("link", { name: chinese ? "关于" : "About", exact: true }).click();
      await expect(page).toHaveURL("/settings/updates");
      await expect(page.getByRole("heading", { name: chinese ? "关于" : "About", level: 1 })).toBeVisible();
      await expect(page.getByText(chinese ? "当前版本：1.0.0" : "Current version: 1.0.0", { exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: chinese ? "检查更新" : "Check for updates", exact: true })).toBeEnabled();
    }
    await page.screenshot({ path: testInfo.outputPath("settings-sidebar-footer.png") });
    for (const height of [768, 320]) {
      await page.setViewportSize({ width: 1024, height });
      for (const collapsed of [false, true]) {
        const toggle = page.getByRole("button", {
          name: chinese
            ? collapsed ? "收起侧边栏" : "展开侧边栏"
            : collapsed ? "Collapse sidebar" : "Expand sidebar",
          exact: true,
        });
        if (await toggle.count()) await toggle.click();
        await nav.getByRole("link").last().scrollIntoViewIfNeeded();
        await page.getByRole("main").evaluate(node => node.scrollTo(0, node.scrollHeight));
        await expect(back).toBeInViewport();
        await expect.poll(bottomGap).toBeLessThanOrEqual(16);
        await expect.poll(bottomGap).toBeGreaterThanOrEqual(0);
      }
    }
    await back.press("Enter");
    await expect(page).toHaveURL("/");
  });
}

test("settings action bars stay flush with the pane bottom throughout scrolling", async ({ page }, testInfo) => {
  const main = page.getByRole("main");
  const actions = page.locator('[data-slot="settings.actions"]');
  for (const path of [
    "/settings", "/settings/providers", "/settings/defaults", "/settings/advanced",
    `/projects/${pid}/settings`,
  ]) {
    await page.goto(path);
    await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toBeEnabled();
    if (path.startsWith("/projects/")) {
      await page.locator("summary").filter({ hasText: "Advanced YAML configuration" }).click();
    }
    for (const height of [768, 320]) {
      await page.setViewportSize({ width: 1024, height });
      for (const progress of [0, 0.5, 1]) {
        await main.evaluate((node, progress) => {
          node.scrollTop = (node.scrollHeight - node.clientHeight) * progress;
        }, progress);
        await expect.poll(() => actions.evaluate(node => {
          const pane = node.closest("main")!.getBoundingClientRect();
          return Math.abs(pane.bottom - node.getBoundingClientRect().bottom);
        })).toBeLessThanOrEqual(1);
      }
      const lastControl = page.locator(
        '[data-slot="settings.section"] input:visible, [data-slot="settings.section"] textarea:visible, [data-slot="settings.section"] button:visible, [data-slot="settings.section"] summary:visible',
      ).last();
      await expect.poll(async () => {
        const control = await lastControl.boundingBox();
        const bar = await actions.boundingBox();
        return !!control && !!bar && control.y + control.height <= bar.y;
      }).toBe(true);
      if (path === "/settings/defaults" && height === 320) {
        await page.screenshot({ path: testInfo.outputPath("settings-actions-scroll-bottom.png") });
      }
    }
  }
});
