import { expect, test } from "@playwright/test";
import {
  fakeApi,
  globalConfiguration,
  configuration,
  pid,
  project,
} from "./fixtures";

const models = {
  ...configuration.registered_models,
  editor: { provider: "second", model: "editor-model" },
};

test("projects only select registered models and operation overrides", async ({
  page,
}) => {
  let saved = structuredClone(configuration);
  await fakeApi(page, {
    [`/projects/${pid}/config`]: { ...saved, registered_models: models },
  });
  await page.route(`**/api/projects/${pid}/config`, async (route) => {
    if (route.request().method() === "PUT") {
      const document = JSON.parse(route.request().postDataJSON().yaml);
      saved = { ...saved, effective: document, yaml: JSON.stringify(document) };
    }
    await route.fulfill({ json: { ...saved, registered_models: models } });
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.getByRole("button", { name: "Add model", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByLabel("API provider", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Model name", { exact: true })).toHaveCount(0);
  await page.getByLabel("Quality tier").selectOption("editor");
  await page
    .locator("summary")
    .filter({ hasText: "Models by operation" })
    .click();
  await page
    .getByLabel("Translate chapters in batches", { exact: true })
    .selectOption("editor");
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Project settings saved", { exact: true }),
  ).toBeVisible();
  expect(Object.keys(saved.effective.llm).sort()).toEqual([
    "budget",
    "routes",
    "tiers",
  ]);
  expect(saved.effective.llm.tiers.strong).toBe("editor");
  expect(saved.effective.llm.routes).toEqual({
    "translation.body": { model: "editor", fallbacks: [] },
  });
  await page.reload();
  await expect(page.getByLabel("Quality tier")).toHaveValue("editor");
  await page
    .getByRole("link", {
      name: "Manage models in global Settings",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Registered providers & models",
      exact: true,
    }),
  ).toBeVisible();
});

test("global defaults persist independently of existing project settings", async ({
  page,
}, testInfo) => {
  let saved = structuredClone(globalConfiguration);
  let writes = 0;
  await fakeApi(page);
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") {
      const input = route.request().postDataJSON();
      expect(input.revision).toBe(saved.revision);
      writes++;
      saved = {
        ...input,
        effective: JSON.parse(input.yaml),
        revision: saved.revision + 1,
      };
    }
    await route.fulfill({ json: saved });
  });
  await page.goto("/settings");
  await page.getByLabel("Default workflow template").selectOption("快速出稿");
  await page.getByLabel("Polishing", { exact: true }).uncheck();
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Global settings saved", { exact: true }),
  ).toBeVisible();
  expect(writes).toBe(1);
  expect(saved.effective.pipeline.polish).toBe(false);
  await page.reload();
  await expect(page.getByLabel("Default workflow template")).toHaveValue(
    "快速出稿",
  );
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
  await page.screenshot({
    path: testInfo.outputPath("global-settings.png"),
    fullPage: true,
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(page.getByLabel("Polishing", { exact: true })).toBeChecked();
  await expect(page.getByLabel("Default workflow template")).toHaveCount(0);
});

test("creation follows the global default template", async ({ page }) => {
  await fakeApi(page, {
    "/strategies/templates": [
      { name: "标准翻译", description: "", steps: {}, recommended: false },
      { name: "快速出稿", description: "", steps: {}, recommended: true },
    ],
  });
  await page.route("**/api/projects", async (route) => {
    const form = await new Response(
      new Uint8Array(route.request().postDataBuffer()!),
      {
        headers: { "content-type": route.request().headers()["content-type"] },
      },
    ).formData();
    expect(JSON.parse(String(form.get("project"))).strategy).toEqual({
      template: "快速出稿",
    });
    await route.fulfill({ json: { ...project, status: "parsing" } });
  });
  await page.goto("/projects/new");
  await expect(page.getByLabel("Translation workflow")).toHaveValue("快速出稿");
  await page.getByLabel("Project name", { exact: true }).fill("New book");
  await page.getByLabel("Upload source", { exact: true }).setInputFiles({
    name: "book.epub",
    mimeType: "application/epub+zip",
    buffer: Buffer.from("fixture"),
  });
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`project=${pid}`));
});

test("global model registration works in Chinese on mobile", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page);
  await page.goto("/settings");
  await page.locator("summary").filter({ hasText: "API 供应商与模型" }).click();
  await expect(
    page.getByLabel("API Key 环境变量", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  await expect(page.getByLabel("模型名称", { exact: true })).toHaveCount(2);
  await page.screenshot({
    path: testInfo.outputPath("mobile-global-models.png"),
    fullPage: true,
  });
  expect(
    await page
      .locator("main")
      .evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
});
