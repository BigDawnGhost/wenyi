import { expect, test } from "@playwright/test";
import { capabilities, configuration, fakeApi, globalConfiguration, pid, project } from "../../sandbox/fixtures";
import { chooseOption } from "./select-helper";
import { expectCardSurface, expectTokenColor } from "./color-helper";


/** Whether the Desktop host must keep running this suite as well.
 *
 * Desktop adds Diagnostics and feedback and About categories; the assertions depend on the host argument.
 */
export const desktopHostSensitive = true;

export function registerTests({
  manualKeys = false,
  categories = {
    en: ["General", "Models & connections", "Stage models", "Workflow", "PDF", "Advanced"],
    "zh-CN": ["通用", "模型与接口", "各阶段模型", "翻译流程", "PDF", "高级"],
  },
}: { manualKeys?: boolean; categories?: Record<"en" | "zh-CN", string[]> } = {}) {
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
      const providerHeading = page.getByRole("heading", {
        name: chinese ? "模型" : "Models", exact: true,
      });
      await expect(providerHeading).toBeVisible();
      await expect(providerHeading).toHaveCSS("font-size", "14px");
      const reference = await providerHeading.evaluate(typography);
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
      await expect(main.getByRole("region", { name: "Workflow", exact: true }).getByRole("switch")).toHaveCount(6);
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
        await expect(section).toHaveCount(path.startsWith("/projects/") ? 5 : 1);
        const content = section.locator('p:visible, h4:visible, input:visible, textarea:visible, a:visible, [role="combobox"]:visible, [role="switch"]:visible');
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
    const editLabel = chinese ? "编辑提供商 deepseek" : "Edit provider deepseek";
    const edit = page.getByRole("button", { name: editLabel, exact: true });
    await expect(edit).toBeVisible();
    await expect(edit).toHaveAttribute("title", editLabel);
    await expect(edit).toHaveText("");
    await expect(page.getByText("deepseek-flash", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel(chinese ? "模型 ID" : "Model ID", { exact: true })).toHaveCount(0);
    await expect.poll(() => page.getByRole("main").evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("settings-providers.png") });
    await page.getByRole("button", { name: chinese ? "返回" : "Back", exact: true }).click();
    await expect(page).toHaveURL(origin);
  });
}

test("direct settings links have a safe return destination", async ({ page }) => {
  await page.goto("/settings/providers");
  await expect(page.getByRole("button", { name: "Edit provider deepseek" })).toBeVisible();
  await page.getByRole("link", { name: "Stage models", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page).toHaveURL("/");
});

test("models in use cannot be deleted and do not show their route references", async ({ page }) => {
  const base = globalConfiguration.effective;
  const effective = { ...base, llm: { ...base.llm, models: {
    ...base.llm.models, "deepseek/alternate": {},
  } } };
  await page.route("**/api/settings", route => route.fulfill({ json: {
    ...globalConfiguration, effective, yaml: JSON.stringify(effective),
  } }));
  await page.goto("/settings/defaults");
  const stage = page.getByRole("combobox", { name: "Model: Translate chapters in batches", exact: true });
  await chooseOption(stage, "deepseek / alternate");
  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Delete model deepseek-flash" })).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "Delete model alternate" })).toBeDisabled();
  await expect(dialog.getByText("translation.body", { exact: false })).toHaveCount(0);
});

test("saving an edited provider returns focus to its edit button", async ({ page }) => {
  let saved = globalConfiguration;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/settings", async route => {
    if (route.request().method() === "PUT") {
      await gate;
      const { yaml } = route.request().postDataJSON();
      saved = { ...saved, yaml, effective: JSON.parse(yaml), revision: saved.revision + 1 };
    }
    await route.fulfill({ json: saved });
  });
  await page.goto("/settings/providers");
  const edit = page.getByRole("button", { name: "Edit provider deepseek" });
  await edit.focus();
  await edit.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Connection ID")).toBeFocused();
  await dialog.getByLabel("API base URL").fill("https://example.invalid/v1");
  await dialog.getByRole("button", { name: "Save provider" }).click();
  try {
    await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
  } finally {
    release();
  }
  await expect(dialog).toHaveCount(0);
  await expect(edit).toBeFocused();
});

for (const theme of ["light", "dark"]) {
  test(`provider editor groups and aligns connection and model controls in ${theme}`, async ({ page }, testInfo) => {
    if (manualKeys) await page.route("**/api/desktop/credentials", route => route.fulfill({ json: {} }));
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(theme => {
      localStorage.setItem("wenyi.locale", "zh-CN");
      localStorage.setItem("wenyi.theme", theme);
    }, theme);
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/settings/providers");
    await page.getByRole("button", { name: "编辑提供商 deepseek" }).click();
    const dialog = page.getByRole("dialog");
    const connection = dialog.getByRole("region", { name: "API 连接", exact: true });
    const models = dialog.getByRole("region", { name: "模型", exact: true });
    const panel = (region: typeof dialog) => region.locator('[data-slot="card"]');
    const id = panel(connection).getByLabel("连接 ID", { exact: true });
    const model = panel(models).getByRole("combobox", { name: "模型名称", exact: true });
    await expect(id).toBeVisible();
    await expect(model).toBeVisible();
    const credential = connection.getByLabel(manualKeys ? "API 密钥" : "API Key 环境变量", { exact: true });
    await expect(credential).toBeVisible();
    await expect(dialog.getByRole("alert")).toHaveCount(0);
    await expect(dialog.getByLabel(manualKeys ? "API Key 环境变量" : "API 密钥", { exact: true })).toHaveCount(0);
    await expect(dialog.getByRole("heading", { name: "环境变量", exact: true })).toHaveCount(0);
    const bounds = (await id.boundingBox())!;
    for (const field of [connection.getByRole("combobox"), credential, model]) {
      const box = (await field.boundingBox())!;
      expect(box.x).toBe(bounds.x);
      expect(box.height).toBe(bounds.height);
    }
    const remove = models.getByRole("button", { name: /删除模型/ });
    expect((await remove.boundingBox())!.y).toBe((await model.boundingBox())!.y);
    await expect(dialog.getByRole("button", { name: "保存提供商" })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`provider-layout-${theme}.png`) });
  });
}

test("model suggestions float without resizing the card and stay keyboard accessible", async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 560 });
  await page.route("**/api/settings/models/list", route => route.fulfill({ json: {
    ok: true, category: "ok", models: Array.from({ length: 24 }, (_, i) => `model-${i}`),
  } }));
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  const dialog = page.getByRole("dialog");
  const name = dialog.getByRole("combobox", { name: "Model name", exact: true });
  const card = dialog.locator('[data-slot="card"]').filter({ has: page.getByRole("combobox", { name: "Model name" }) });
  await dialog.getByRole("button", { name: "Fetch models" }).click();
  const options = page.getByRole("listbox", { name: "Model name" });
  await expect(options).toBeVisible();
  await dialog.getByLabel("Connection ID").focus();
  await expect(options).toHaveCount(0);
  const height = await card.evaluate(el => el.clientHeight);
  await name.focus();
  await expect(options).toBeInViewport();
  expect(await card.evaluate(el => el.clientHeight)).toBe(height);
  await name.press("Escape");
  await expect(options).toHaveCount(0);
  await expect(name).toBeFocused();
  await name.press("ArrowDown");
  await name.press("Enter");
  await expect(name).toHaveValue("model-0");
});

test("stage models can retry an initial capability failure in place", async ({ page }) => {
  let unavailable = true;
  await page.route("**/api/capabilities", route => unavailable
    ? route.fulfill({ status: 503, json: { detail: "Stages unavailable" } })
    : route.fulfill({ json: capabilities }));
  await page.goto("/settings/defaults");
  const section = page.getByRole("region", { name: "Stage models", exact: true });
  await expect(section.getByRole("alert")).toContainText("Stages unavailable");
  unavailable = false;
  await section.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(section.getByRole("combobox", { name: "Thinking effort: Verify review evidence" })).toBeEnabled();
  await expect(section.getByRole("alert")).toHaveCount(0);
});

test("provider dialog saves multiple models directly and cancellation writes nothing", async ({ page }, testInfo) => {
  let saved = structuredClone(globalConfiguration);
  let writes = 0;
  await page.route("**/api/settings", async route => {
    if (route.request().method() === "PUT") {
      writes++;
      const { yaml } = route.request().postDataJSON();
      saved = { ...saved, yaml, effective: JSON.parse(yaml), revision: saved.revision + 1 };
    }
    await route.fulfill({ json: saved });
  });
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Add provider", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Connection ID").fill("editorial");
  await expect(dialog.getByLabel("Model ID", { exact: true })).toHaveCount(0);
  await dialog.getByLabel("API base URL").fill("https://example.invalid/v1");
  await dialog.getByLabel("Model name", { exact: true }).fill("writer-v1");
  await dialog.getByRole("button", { name: "Add model", exact: true }).click();
  await dialog.getByLabel("Model name", { exact: true }).last().fill("verifier-v2");
  await page.screenshot({ path: testInfo.outputPath("provider-editor.png") });
  await dialog.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("button", { name: "Edit provider editorial" })).toBeVisible();
  await expect(page.getByText("writer-v1", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("API base URL")).toHaveCount(0);
  await page.getByRole("button", { name: "Edit provider editorial" }).click();
  await dialog.getByRole("button", { name: "Delete model writer-v1" }).click();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(writes).toBe(1);
  expect(saved.effective.llm.models).toMatchObject({
    "editorial/writer-v1": {},
    "editorial/verifier-v2": {},
  });
  await page.reload();
  await page.getByRole("button", { name: "Edit provider editorial" }).click();
  await expect(dialog.getByLabel("Model name", { exact: true }).last()).toHaveValue("verifier-v2");
});

test("basic provider edits preserve parameters configured in advanced YAML", async ({ page }) => {
  const initial = globalConfiguration.effective;
  const provider = { ...initial.llm.providers.deepseek, timeout: 45.5, max_retries: 0, max_concurrency: 3 };
  const model = {
    ...initial.llm.models["deepseek/deepseek-flash"],
    max_output_tokens: 8192,
    options: { thinking: true, reasoning_effort: "low", extra_body: { temperature: 0.4 } },
  };
  let effective = {
    ...initial,
    llm: { ...initial.llm, providers: { deepseek: provider }, models: { "deepseek/deepseek-flash": model } },
  };
  await page.route("**/api/settings", route => {
    if (route.request().method() === "PUT") effective = JSON.parse(route.request().postDataJSON().yaml);
    return route.fulfill({ json: { ...globalConfiguration, effective, yaml: JSON.stringify(effective) } });
  });
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("spinbutton")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Add parameter" })).toHaveCount(0);
  await dialog.getByLabel("API base URL").fill("https://example.invalid/v1");
  await dialog.getByLabel("Model name", { exact: true }).fill("updated-model");
  await dialog.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByText("Global settings saved", { exact: true })).toBeVisible();
  expect(effective.llm.providers.deepseek).toEqual({ ...provider, base_url: "https://example.invalid/v1" });
  expect(effective.llm.models).toEqual({ "deepseek/updated-model": model });
  expect(Object.values(effective.llm.routes).map(route => route.model)).toEqual(
    Object.keys(effective.llm.routes).map(() => "deepseek/updated-model"),
  );
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
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
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
  const polish = page.getByRole("switch", { name: "Polishing", exact: true });
  // Before settings arrive the disabled placeholder is already unchecked.
  await expect(polish).toBeChecked();
  await polish.uncheck();
  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await page.getByLabel("Model name", { exact: true }).fill("unfinished model");
  await page.getByLabel("API provider", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL("/settings/workflow");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(polish).not.toBeChecked();
});

for (const chinese of [false, true]) {
  test(`standalone settings keep tuning in advanced YAML in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto("/settings/workflow");
    const main = page.getByRole("main");
    await expect(main.getByRole("switch")).toHaveCount(6);
    const workflow = main.getByRole("region", { name: chinese ? "翻译流程" : "Workflow", exact: true });
    await expect(workflow.getByRole("spinbutton", { includeHidden: true })).toHaveCount(0);
    await expect(page.getByLabel(chinese ? "逐段注释定位" : "Paragraph annotation alignment", { exact: true })).toHaveCount(0);
    await expect(main.locator("details, summary")).toHaveCount(0);
    await page.getByLabel(chinese ? "润色" : "Polishing", { exact: true }).uncheck();
    await page.screenshot({ path: testInfo.outputPath("settings-workflow-simple.png") });
    await page.getByRole("link", { name: chinese ? "各阶段模型" : "Stage models", exact: true }).click();
    await expect(main.getByRole("combobox")).toHaveCount(6);
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
    await expect(main.getByRole("combobox")).toHaveCount(11);
    await expect(main.getByRole("switch")).toHaveCount(8);
    await expect(main.getByRole("spinbutton", { includeHidden: true })).toHaveCount(0);
    await expect(page.getByLabel(chinese ? "分批翻译章节" : "Translate chapters in batches", { exact: true })).toHaveCount(0);
    await expect(main.locator("details, summary")).toHaveCount(0);
    await expect(main.getByRole("table")).toHaveCount(0);
    await expect(main.getByRole("heading", { level: 2 })).toHaveText(
      chinese
        ? ["各阶段模型", "翻译风格", "翻译流程", "导出偏好", advancedName]
        : ["Stage models", "Translation style", "Workflow", "Export preferences", advancedName],
    );
    const polishMode = page.getByRole("combobox", { name: chinese ? "润色模式" : "Polishing mode", exact: true });
    const chapterConcurrency = page.getByRole("combobox", { name: chinese ? "章节并行数" : "Chapter concurrency", exact: true });
    await expect(polishMode).toBeEnabled();
    await expect(chapterConcurrency).toBeDisabled();
    await chooseOption(polishMode, chinese ? "稀疏精修" : "Sparse semantic edit");
    expect(JSON.parse(await yaml.inputValue()).pipeline.polish_mode).toBe("sparse");
    await chooseOption(
      page.getByRole("combobox", { name: chinese ? "术语模式" : "Glossary mode", exact: true }),
      chinese ? "全书预编译词典" : "Compiled Book Bible",
    );
    await expect(chapterConcurrency).toBeEnabled();
    await chooseOption(chapterConcurrency, "4");
    const consistencySweep = page.getByLabel(chinese ? "全书一致性巡检" : "Book-wide consistency sweep", { exact: true });
    await consistencySweep.check();
    const polish = page.getByLabel(chinese ? "润色" : "Polishing", { exact: true });
    await polish.uncheck();
    await expect(polishMode).toBeDisabled();
    expect(JSON.parse(await yaml.inputValue()).pipeline).toMatchObject({
      polish: false,
      glossary_mode: "compiled",
      polish_mode: "sparse",
      chapter_concurrency: 4,
      consistency_sweep: true,
    });
    await chooseOption(
      page.getByRole("combobox", { name: chinese ? "批次术语范围" : "Batch glossary", exact: true }),
      chinese ? "仅当前批次相关" : "Relevant to each batch",
    );
    expect(JSON.parse(await yaml.inputValue()).pipeline.batch_glossary).toBe("relevant");
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
          sections.length === 5 && sections.every(section => {
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
  test(`${scope} stage thinking shows the model's effort and saves concrete selections`, async ({ page }) => {
    const base = scope === "global" ? globalConfiguration : configuration;
    const endpoint = scope === "global" ? "/settings" : `/projects/${pid}/config`;
    let saved = {
      ...base,
      registered_models: {
        ...base.registered_models,
        // Return an unsorted capability list to verify presentation order.
        "deepseek/deepseek-flash": {
          ...base.registered_models["deepseek/deepseek-flash"],
          thinking_levels: ["max", "high", "low", "xhigh", "off"],
        },
      },
    };
    const writes: Record<string, { model: string; thinking?: string | null }>[] = [];
    await page.route(`**/api${endpoint}`, async route => {
      if (route.request().method() === "PUT") {
        const { yaml } = route.request().postDataJSON();
        const effective = JSON.parse(yaml);
        writes.push(effective.llm.routes);
        saved = { ...saved, effective, yaml };
      }
      await route.fulfill({ json: saved });
    });
    await page.goto(scope === "global" ? "/settings/defaults" : `/projects/${pid}/settings`);
    const translation = page.getByRole("combobox", { name: "Thinking effort: Translate chapters in batches" });
    const verify = page.getByRole("combobox", { name: "Thinking effort: Verify review evidence" });
    await expect(translation).toHaveText("high");
    await expect(page.getByText("Thinking effort", { exact: true })).toHaveCount(0);
    await translation.click();
    await expect(page.getByRole("option")).toHaveText(["off", "low", "high", "xhigh", "max"]);
    await expect(page.getByRole("option", { name: "high", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("option", { name: "max", exact: true }).click();
    await expect(verify).toHaveText("high");
    await page.getByRole("button", { name: "Save configuration", exact: true }).click();
    await expect(page.getByText(scope === "global" ? "Global settings saved" : "Project settings saved", { exact: true })).toBeVisible();
    expect(writes[0]["translation.body"]).toMatchObject({
      model: "deepseek/deepseek-flash", thinking: "max",
    });
    expect(writes[0]["review.verify"].thinking).toBeUndefined();
    await page.reload();
    await expect(translation).toHaveText("max");
    await chooseOption(translation, "high");
    await page.getByRole("button", { name: "Save configuration", exact: true }).click();
    await expect.poll(() => writes[1]?.["translation.body"].thinking).toBe("high");
  });

  test(`${scope} basic edits preserve advanced values before and after YAML validation`, async ({ page }) => {
    const base = scope === "global" ? globalConfiguration : configuration;
    const models = {
      ...configuration.registered_models,
      "deepseek/editor-model": { provider: "deepseek", model: "editor-model", thinking_levels: [], thinking_value: null },
    };
    const document = {
      ...base.effective,
      llm: {
        ...base.effective.llm,
        ...(scope === "global" ? { models: Object.fromEntries(Object.keys(models).map(ref => [ref, {}])), preset: null } : {}),
        routes: {
          ...base.effective.llm.routes,
          "translation.body": { model: "deepseek/editor-model", fallbacks: ["deepseek/deepseek-flash"] },
        },
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
    await expect(page.getByRole("combobox", { name: "Default model", exact: true })).toHaveCount(0);
    const review = page.getByRole("combobox", { name: "Model: Verify review evidence" });
    const autofix = page.getByRole("combobox", { name: "Model: Verify autofixes" });
    await chooseOption(review, "deepseek / editor-model");
    await expect(autofix).toHaveText("deepseek / deepseek-flash");
    await autofix.click();
    await expect(page.getByRole("option")).toHaveText(["deepseek / deepseek-flash", "deepseek / editor-model"]);
    await page.keyboard.press("Escape");
    if (scope === "global") await page.getByRole("link", { name: "Workflow", exact: true }).click();
    await page.getByLabel("Apply autofixes to the saved translation after review", { exact: true }).uncheck();
    const save = page.getByRole("button", { name: "Save configuration", exact: true });
    await save.click();
    await expect.poll(() => writes).toBe(1);
    await expect(save).not.toHaveAttribute("aria-busy", "true");
    const expected = {
      ...document,
      llm: {
        ...document.llm,
        routes: { ...document.llm.routes, "review.verify": { model: "deepseek/editor-model" } },
      },
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
    await expect.poll(async () => JSON.parse(await yaml.inputValue())).toEqual(tuned);
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

test("an in-flight project validation does not replace a newer YAML edit", async ({ page }) => {
  const endpoint = `**/api/projects/${pid}/config/validate`;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(endpoint, async route => {
    const { yaml } = route.request().postDataJSON();
    await gate;
    await route.fulfill({ json: { ...configuration, yaml, effective: JSON.parse(yaml) } });
  });
  await page.goto(`/projects/${pid}/settings`);
  const editor = page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true });
  const validate = page.getByRole("button", { name: "Validate configuration", exact: true });
  await expect(editor).toBeEditable();
  const original = JSON.parse(await editor.inputValue());
  const draft = JSON.stringify({ ...original, segment: { max_tokens_per_batch: 2700 } });
  const newer = JSON.stringify({ ...original, segment: { max_tokens_per_batch: 3100 } });
  await editor.fill(draft);
  const response = page.waitForResponse(endpoint);
  try {
    await validate.click();
    await expect(validate).toHaveAttribute("aria-busy", "true");
    await editor.fill(newer);
  } finally {
    release();
  }
  await response;
  await expect(validate).not.toHaveAttribute("aria-busy", "true");
  await expect(editor).toHaveValue(newer);
  await expect(page.locator('[id="route-translation.body"]')).toBeDisabled();
});

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
  await expect(page.locator('[id="route-translation.body"]')).toBeDisabled();
  const switches = page.getByRole("main").getByRole("switch");
  await expect(switches).toHaveCount(8);
  for (const control of await switches.all()) {
    await expect(control).toBeDisabled();
  }
  await expect(page.getByLabel("Bilingual order", { exact: true })).toBeDisabled();
  await expect(page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true })).toBeDisabled();
  for (const name of ["Save configuration", "Restore defaults", "Validate configuration"]) {
    await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
  }
  await expect(page.getByText("Read-only while a task runs", { exact: true })).toHaveCount(0);
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
