import { expect, test } from "@playwright/test";
import { configuration, fakeApi, globalConfiguration, pid, project } from "../../sandbox/fixtures";
import { chooseOption } from "./select-helper";
import { expectCardSurface, expectTokenColor } from "./color-helper";


/** Whether the Desktop host must keep running this suite as well.
 *
 * Desktop adds Diagnostics and feedback and About categories; the assertions depend on the host argument.
 */
export const desktopHostSensitive = true;

export function registerTests({
  categories = {
    en: ["General", "Models & connections", "Default models", "Workflow", "PDF", "Advanced"],
    "zh-CN": ["通用", "模型与接口", "默认模型", "翻译流程", "PDF", "高级"],
  },
}: { categories?: Record<"en" | "zh-CN", string[]> } = {}) {
test.beforeEach(async ({ page }) => {
  await fakeApi(page);
});

for (const theme of ["light", "dark"]) {
  for (const chinese of [false, true]) {
    test(`settings subsection headings match the API connection heading in ${chinese ? "Chinese" : "English"} in ${theme}`, async ({ page }) => {
      await page.addInitScript(({ locale, theme }) => {
        localStorage.setItem("wenyi.locale", locale);
        localStorage.setItem("wenyi.theme", theme);
      }, { locale: chinese ? "zh-CN" : "en", theme });
      const typography = (element: Element) => {
        const style = getComputedStyle(element);
        return {
          family: style.fontFamily,
          size: style.fontSize,
          weight: style.fontWeight,
          lineHeight: style.lineHeight,
          color: style.color,
        };
      };
      await page.goto("/settings/providers");
      const reference = await page.getByRole("heading", {
        name: chinese ? "API 连接" : "Connections", exact: true,
      }).evaluate(typography);
      for (const path of ["/settings", "/settings/pdf", "/settings/advanced", `/projects/${pid}/settings`]) {
        await page.goto(path);
        const headings = page.locator("main h2:visible, main h3:visible");
        await expect(headings.first()).toBeVisible();
        for (const heading of await headings.all()) {
          expect(await heading.evaluate(typography)).toEqual(reference);
        }
      }
    });
  }
}

for (const theme of ["light", "dark"]) {
  test(`workflow options use labeled switches in ${theme}`, async ({ page }, testInfo) => {
    await page.addInitScript(theme => localStorage.setItem("wenyi.theme", theme), theme);
    for (const scope of ["global", "project"]) {
      await page.goto(scope === "global" ? "/settings/workflow" : `/projects/${pid}/settings`);
      const main = page.getByRole("main");
      await expect(main.getByRole("region", { name: "Workflow", exact: true }).getByRole("switch")).toHaveCount(4);
      await expect(main.getByRole("checkbox")).toHaveCount(0);
      for (const [key, name] of [
        ["book_understanding", "Book understanding"],
        ["polish", "Polishing"],
        ["review", "Whole-book review"],
        ["review_autofix", "Apply autofixes to the saved translation after review"],
      ]) {
        const control = main.getByRole("switch", { name, exact: true });
        await expect(control).toBeChecked();
        await expectTokenColor(control, "background-color", "primary");
        await control.focus();
        await page.keyboard.press("Space");
        await expect(control).not.toBeChecked();
        await expectTokenColor(control, "background-color", "input");
        await page.keyboard.press("Enter");
        await expect(control).toBeChecked();
        await main.locator(`label[for="pipeline-${key}"]`).click();
        await expect(control).not.toBeChecked();
      }
      await page.screenshot({ path: testInfo.outputPath(`workflow-switches-${scope}-${theme}.png`) });
      if (scope === "global") {
        await page.getByRole("link", { name: "Advanced", exact: true }).click();
      }
      const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true });
      expect(JSON.parse(await yaml.inputValue()).pipeline).toMatchObject({
        book_understanding: false, polish: false, review: false, review_autofix: false,
      });
    }
  });
}

for (const theme of ["light", "dark"]) {
  test(`every settings category contains its content in panels in ${theme}`, async ({ page }, testInfo) => {
    await page.addInitScript(theme => localStorage.setItem("wenyi.theme", theme), theme);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/settings");
    const navigation = page.getByRole("navigation", { name: "Settings navigation", exact: true });
    await expect(navigation.getByRole("link")).toHaveCount(categories.en.length);
    const paths = await navigation.getByRole("link").evaluateAll(links =>
      links.map(link => link.getAttribute("href")!),
    );
    paths.push(`/projects/${pid}/settings`);
    for (const path of paths) {
      await test.step(path, async () => {
        await page.goto(path);
        const section = page.locator('[data-slot="settings.section"]:visible');
        await expect(section).toHaveCount(path.startsWith("/projects/") ? 4 : 1);
        const content = section.locator('p:visible, input:visible, textarea:visible, a:visible, [role="combobox"]:visible, [role="switch"]:visible');
        await expectCardSurface(content);
        await expect.poll(() => page.getByRole("main").evaluate(node =>
          node.scrollWidth <= node.clientWidth,
        )).toBe(true);
        await page.screenshot({
          path: testInfo.outputPath(`surface-${path.split("/").pop()}-${theme}.png`),
        });
      });
    }
  });
}

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
    await expect(nav.getByRole("link")).toHaveCount(categories.en.length);
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

test("model suggestions remain pointer-accessible outside the model card", async ({ page }) => {
  // Leave room below both hosts' forms to isolate card clipping from viewport edges.
  await page.setViewportSize({ width: 1280, height: 1200 });
  await page.route("**/settings/models/list", route => route.fulfill({
    json: {
      ok: true,
      models: Array.from({ length: 7 }, (_, index) => `listed-model-${index + 1}`),
      category: "ok",
      message: "",
    },
  }));
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Fetch models", exact: true }).click();
  const option = page.getByRole("option", { name: "listed-model-7", exact: true });
  await expect(option).toBeInViewport();
  // A locator click scrolls overflow-hidden ancestors programmatically, masking clipping.
  await expect.poll(() => option.evaluate(node => {
    const bounds = node.getBoundingClientRect();
    return node.contains(document.elementFromPoint(
      bounds.x + bounds.width / 2, bounds.y + bounds.height / 2,
    ));
  })).toBe(true);
  const bounds = (await option.boundingBox())!;
  await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await expect(page.getByRole("combobox", { name: "Model name", exact: true }))
    .toHaveValue("listed-model-7");
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
  test(`standalone settings keep tuning in advanced YAML in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/settings/workflow");
    const main = page.getByRole("main");
    await expect(main.getByRole("switch")).toHaveCount(4);
    const workflow = main.getByRole("region", { name: chinese ? "翻译流程" : "Workflow", exact: true });
    await expect(workflow.getByRole("spinbutton", { includeHidden: true })).toHaveCount(0);
    await expect(page.getByLabel(chinese ? "逐段注释定位" : "Paragraph annotation alignment", { exact: true })).toHaveCount(0);
    await expect(main.locator("details, summary")).toHaveCount(0);
    await page.getByLabel(chinese ? "润色" : "Polishing", { exact: true }).uncheck();
    await page.screenshot({ path: testInfo.outputPath("settings-workflow-simple.png") });
    await page.getByRole("link", { name: chinese ? "默认模型" : "Default models", exact: true }).click();
    await expect(main.getByRole("combobox")).toHaveCount(3);
    await expect(page.getByLabel(chinese ? "分批翻译章节" : "Translate chapters in batches", { exact: true })).toHaveCount(0);
    await page.getByRole("link", { name: chinese ? "高级" : "Advanced", exact: true }).click();
    const yaml = page.getByRole("textbox", { name: chinese ? "高级 YAML 配置" : "Advanced YAML configuration", exact: true });
    await expect(yaml).toBeVisible();
    const draft = await yaml.inputValue();
    expect(JSON.parse(draft).pipeline.polish).toBe(false);
    expect(JSON.parse(draft).segment.max_tokens_per_batch).toBe(1800);
    await yaml.fill(`${draft}\n# unsaved draft`);
    await page.getByRole("link", { name: chinese ? "通用" : "General", exact: true }).click();
    await page.getByRole("link", { name: chinese ? "高级" : "Advanced", exact: true }).click();
    await expect(yaml).toHaveValue(`${draft}\n# unsaved draft`);
  });
}

for (const chinese of [false, true]) {
  test(`project settings show advanced YAML directly below the simple form in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
    const routeRequests: string[] = [];
    page.on("request", request => {
      if (request.url().includes(`/projects/${pid}/models`)) routeRequests.push(request.url());
    });
    await page.goto(`/projects/${pid}/settings`);
    const main = page.getByRole("main");
    const advancedName = chinese ? "高级 YAML 配置" : "Advanced YAML configuration";
    const yaml = page.getByRole("textbox", { name: advancedName, exact: true });
    await expect(yaml).toBeVisible();
    await expect(page.getByRole("link", { name: advancedName, exact: true })).toHaveCount(0);
    await expect(main.getByRole("combobox")).toHaveCount(4);
    await expect(main.getByRole("switch")).toHaveCount(7);
    await expect(main.getByRole("spinbutton", { includeHidden: true })).toHaveCount(0);
    await expect(page.getByLabel(chinese ? "分批翻译章节" : "Translate chapters in batches", { exact: true })).toHaveCount(0);
    await expect(main.locator("details, summary")).toHaveCount(0);
    await expect(main.getByRole("table")).toHaveCount(0);
    await expect(main.getByRole("heading", { level: 2 })).toHaveText(
      chinese ? ["模型", "翻译流程", "导出偏好", advancedName] : ["Models", "Workflow", "Export preferences", advancedName],
    );
    const polish = page.getByLabel(chinese ? "润色" : "Polishing", { exact: true });
    await polish.uncheck();
    expect(JSON.parse(await yaml.inputValue()).pipeline.polish).toBe(false);
    expect(routeRequests).toEqual([]);
    const advanced = main.getByRole("region", { name: advancedName, exact: true });
    const workflow = main.getByRole("region", { name: chinese ? "翻译流程" : "Workflow", exact: true });
    for (const width of [1024, 1600]) {
      await page.setViewportSize({ width, height: 1080 });
      await expect.poll(async () => {
        const editor = await advanced.boundingBox();
        const section = await workflow.boundingBox();
        return !!editor && !!section &&
          editor.y >= section.y + section.height &&
          Math.abs(editor.x - section.x) <= 1;
      }).toBe(true);
      await expect.poll(() => main.evaluate(node => {
        const container = node.querySelector('[data-slot="page.container"]')!;
        const sections = [...node.querySelectorAll('[data-slot="settings.section"]')];
        const bounds = container.getBoundingClientRect();
        const pane = node.getBoundingClientRect();
        return bounds.width === Math.min(node.clientWidth, 1024) &&
          Math.abs(bounds.left - pane.left - (node.clientWidth - bounds.width) / 2) <= 1 &&
          node.scrollWidth === node.clientWidth &&
          sections.length === 4 && sections.every(section => {
            const heading = section.querySelector(":scope > h2")!.getBoundingClientRect();
            const content = section.querySelector(":scope > div")!.getBoundingClientRect();
            const bounds = section.getBoundingClientRect();
            return heading.bottom <= content.top &&
              Math.abs(content.left - bounds.left) <= 1 &&
              Math.abs(content.width - bounds.width) <= 1;
          });
      })).toBe(true);
    }
    await yaml.scrollIntoViewIfNeeded();
    await expect(yaml).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath("project-settings-inline-yaml.png") });
    await page.getByRole("button", { name: chinese ? "校验配置" : "Validate configuration", exact: true }).click();
    await expect(page.getByText(chinese ? "配置有效，尚未保存" : "Configuration is valid but not saved yet", { exact: true })).toBeVisible();
    await expect(main.getByRole("table")).toHaveCount(0);
    expect(routeRequests).toEqual([]);
  });
}

for (const scope of ["global", "project"] as const) {
  test(`${scope} basic edits preserve advanced values before and after YAML validation`, async ({ page }) => {
    const base = scope === "global" ? globalConfiguration : configuration;
    const models = {
      ...configuration.registered_models,
      editor: { provider: "default", model: "editor-model" },
    };
    const document = {
      ...base.effective,
      llm: {
        ...base.effective.llm,
        ...(scope === "global" ? { models, preset: null } : {}),
        routes: { "translation.body": { model: "editor", fallbacks: ["default_model"] } },
      },
      segment: { max_tokens_per_batch: 2700, max_tokens_per_segment: 0 },
      pipeline: { ...base.effective.pipeline, review_concurrency: 7, annotation_alignment: false },
    };
    let saved = { ...base, effective: document, yaml: JSON.stringify(document), registered_models: models };
    const endpoint = scope === "global" ? "/settings" : `/projects/${pid}/config`;
    let writes = 0;
    await page.route(`**/api${endpoint}`, async route => {
      if (route.request().method() === "PUT") {
        writes++;
        const { yaml } = route.request().postDataJSON();
        saved = { ...saved, yaml, effective: JSON.parse(yaml) };
      }
      await route.fulfill({ json: saved });
    });
    await page.route(`**/api${endpoint}/validate`, async route => {
      const { yaml } = route.request().postDataJSON();
      await route.fulfill({ json: { ...saved, yaml, effective: JSON.parse(yaml) } });
    });
    await page.goto(scope === "global" ? "/settings/defaults" : `/projects/${pid}/settings`);
    await chooseOption(page.getByLabel("Quality tier", { exact: true }), "default / editor-model");
    if (scope === "global") await page.getByRole("link", { name: "Workflow", exact: true }).click();
    await page.getByLabel("Apply autofixes to the saved translation after review", { exact: true }).uncheck();
    await page.getByRole("button", { name: "Save configuration", exact: true }).click();
    await expect.poll(() => writes).toBe(1);
    await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toBeEnabled();
    const expected = {
      ...document,
      llm: { ...document.llm, tiers: { ...document.llm.tiers, strong: "editor" } },
      pipeline: { ...document.pipeline, review_autofix: false },
    };
    expect(saved.effective).toEqual(expected);
    if (scope === "global") await page.getByRole("link", { name: "Advanced", exact: true }).click();
    const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true });
    const tuned = { ...expected, segment: { ...expected.segment, max_tokens_per_batch: 3100 } };
    await yaml.fill(JSON.stringify(tuned));
    await page.getByRole("button", { name: "Validate configuration", exact: true }).click();
    await expect(page.getByText("Configuration is valid but not saved yet", { exact: true })).toBeVisible();
    expect(writes).toBe(1);
    if (scope === "global") await page.getByRole("link", { name: "Workflow", exact: true }).click();
    await page.getByLabel("Book understanding", { exact: true }).uncheck();
    await page.getByRole("button", { name: "Save configuration", exact: true }).click();
    await expect.poll(() => writes).toBe(2);
    expect(saved.effective).toEqual({
      ...tuned,
      pipeline: { ...tuned.pipeline, book_understanding: false },
    });
  });
}

test("idle project settings remain editable after returning from the event log", async ({ page }) => {
  let saved = configuration;
  let writes = 0;
  await page.route(`**/api/projects/${pid}/config`, async route => {
    if (route.request().method() === "PUT") {
      writes++;
      const { yaml } = route.request().postDataJSON();
      saved = { ...saved, yaml, effective: JSON.parse(yaml) };
    }
    await route.fulfill({ json: saved });
  });
  await page.goto(`/projects/${pid}/settings`);
  const polish = page.getByLabel("Polishing", { exact: true });
  const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true });
  const save = page.getByRole("button", { name: "Save configuration", exact: true });
  await expect(save).toBeEnabled();
  for (let visit = 0; visit < 2; visit++) {
    await page.getByRole("link", { name: "Event log", exact: true }).click();
    await expect(page).toHaveURL(`/projects/${pid}/events`);
    await page.getByRole("link", { name: "Project settings", exact: true }).click();
    await expect(page).toHaveURL(`/projects/${pid}/settings`);
    await expect(polish).toBeEnabled();
    await expect(polish).toBeChecked();
    await expect(yaml).toBeEnabled();
    await expect(yaml).toHaveValue(configuration.yaml);
    for (const name of ["Save configuration", "Restore defaults", "Validate configuration"]) {
      await expect(page.getByRole("button", { name, exact: true })).toBeEnabled();
    }
  }
  await polish.uncheck();
  await save.click();
  await expect(page.getByText("Project settings saved", { exact: true })).toBeVisible();
  expect(writes).toBe(1);
  expect(saved.effective.pipeline.polish).toBe(false);
});

test("running projects keep both basic and advanced settings read-only", async ({ page }) => {
  await fakeApi(page, { [`/projects/${pid}`]: { ...project, status: "translating" } });
  await page.goto(`/projects/${pid}/settings`);
  await expect(page.getByText("Read-only while a task runs", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Quality tier", { exact: true })).toBeDisabled();
  const switches = page.getByRole("main").getByRole("switch");
  await expect(switches).toHaveCount(7);
  for (const control of await switches.all()) {
    await expect(control).toBeDisabled();
  }
  await expect(page.getByLabel("Bilingual order", { exact: true })).toBeDisabled();
  await expect(page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true })).toBeDisabled();
  for (const name of ["Save configuration", "Restore defaults", "Validate configuration"]) {
    await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
  }
});

test("category content uses a centered reading column without a duplicate title", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/providers");
  for (const category of categories.en) {
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
      const bounds = container.getBoundingClientRect();
      const pane = node.getBoundingClientRect();
      return bounds.width === Math.min(node.clientWidth, 1024) &&
        Math.abs(bounds.left - pane.left - (node.clientWidth - bounds.width) / 2) <= 1 &&
        node.scrollWidth === node.clientWidth;
    })).toBe(true);
  }
});

for (const chinese of [false, true]) {
  test(`settings keeps Back at the bottom and uses clear category names in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    await page.addInitScript(locale => {
      localStorage.setItem("wenyi.locale", locale);
    }, chinese ? "zh-CN" : "en");
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
    await expect(nav.getByRole("link")).toHaveText(categories[chinese ? "zh-CN" : "en"]);
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

test("settings action bars float above the pane bottom throughout scrolling", async ({ page }, testInfo) => {
  const main = page.getByRole("main");
  const actions = page.locator('[data-slot="settings.actions"]');
  for (const path of [
    "/settings", "/settings/providers", "/settings/defaults", "/settings/advanced",
    `/projects/${pid}/settings`,
  ]) {
    await page.goto(path);
    await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toBeEnabled();
    for (const height of [768, 320]) {
      await page.setViewportSize({ width: 1024, height });
      for (const progress of [0, 0.5, 1]) {
        await main.evaluate((node, progress) => {
          node.scrollTop = (node.scrollHeight - node.clientHeight) * progress;
        }, progress);
        await expect.poll(() => actions.evaluate(node => {
          const pane = node.closest("main")!.getBoundingClientRect();
          return pane.bottom - node.getBoundingClientRect().bottom;
        })).toBeGreaterThanOrEqual(12);
        await expect.poll(() => actions.evaluate(node => {
          const pane = node.closest("main")!.getBoundingClientRect();
          return pane.bottom - node.getBoundingClientRect().bottom;
        })).toBeLessThanOrEqual(32);
      }
      const lastControl = page.locator(
        '[data-slot="settings.section"] input:visible, [data-slot="settings.section"] textarea:visible, [data-slot="settings.section"] button:visible, [data-slot="settings.section"] summary:visible, [data-slot="page.container"] a:visible',
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
}
