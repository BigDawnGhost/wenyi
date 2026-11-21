import { expect, test } from "@playwright/test";
import { chooseOption } from "../../../packages/ui/tests/e2e/select-helper";
import {
  fakeApi,
  globalConfiguration,
  configuration,
  pid,
  project,
} from "../../../packages/ui/sandbox/fixtures";

const models = {
  ...configuration.registered_models,
  "second/editor-model": { provider: "second", model: "editor-model", thinking_levels: [], thinking_value: null },
};

test("global workflow has no project precision controls or polishing constraint", async ({ page }) => {
  await fakeApi(page, {
    "/settings": {
      ...globalConfiguration,
      effective: {
        ...globalConfiguration.effective,
        pipeline: { ...globalConfiguration.effective.pipeline, translation_mode: "three_draft_synthesis", polish: true },
      },
    },
  });
  await page.goto("/settings/workflow");
  await expect(page.getByLabel("Polishing", { exact: true })).toBeEnabled();
  await expect(page.getByLabel("Translation mode", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Default workflow template", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("option", { name: /Quick draft|快速出稿/ })).toHaveCount(0);
  await expect(page.getByLabel("Tokens per batch", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Parallel initial drafts (1–3)", { exact: true })).toHaveCount(0);
});

test("projects select registered models independently for each operation", async ({
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
  const translation = page.getByRole("combobox", { name: "Model: Translate chapters in batches" });
  await chooseOption(translation, "second / editor-model");
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Project settings saved", { exact: true }),
  ).toBeVisible();
  expect(Object.keys(saved.effective.llm).sort()).toEqual([
    "budget",
    "routes",
  ]);
  expect(saved.effective.llm.routes).toEqual({
    ...configuration.effective.llm.routes, "translation.body": { model: "second/editor-model" },
  });
  await page.reload();
  await expect(translation).toHaveText("second / editor-model");
  await chooseOption(translation, "deepseek / deepseek-flash");
  const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true });
  expect(JSON.parse(await yaml.inputValue()).llm.routes).toEqual(configuration.effective.llm.routes);
  await page
    .getByRole("link", {
      name: "Manage models in global Settings",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Models & connections",
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Stage models", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page).toHaveURL(`/projects/${pid}/settings`);
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
  await page.goto("/settings/workflow");
  await expect(page.getByLabel("Default workflow template")).toHaveCount(0);
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
  await expect(page.getByLabel("Default workflow template")).toHaveCount(0);
  expect(saved.default_template).toBe("标准翻译");
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
  await page.screenshot({
    path: testInfo.outputPath("global-settings.png"),
    fullPage: true,
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(page.getByLabel("Polishing", { exact: true })).toBeChecked();
  await expect(page.getByLabel("Default workflow template")).toHaveCount(0);
});

test("creation uses server defaults without a workflow selector", async ({
  page,
}) => {
  await fakeApi(page);
  const templateRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/strategies/templates"))
      templateRequests.push(request.url());
  });
  await page.route("**/api/projects", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const form = await new Response(
      new Uint8Array(route.request().postDataBuffer()!),
      {
        headers: { "content-type": route.request().headers()["content-type"] },
      },
    ).formData();
    expect(JSON.parse(String(form.get("project")))).not.toHaveProperty(
      "strategy",
    );
    await route.fulfill({ json: { ...project, status: "parsing" } });
  });
  await page.goto("/projects/new");
  await expect(page.getByLabel("Translation workflow")).toHaveCount(0);
  await page.getByLabel("Project name", { exact: true }).fill("New book");
  await page.getByLabel("Upload source", { exact: true }).setInputFiles({
    name: "book.epub",
    mimeType: "application/epub+zip",
    buffer: Buffer.from("fixture"),
  });
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  await expect(page).toHaveURL(`/projects/${pid}`);
  expect(templateRequests).toEqual([]);
});

test("global model registration works in Chinese in a compact desktop window", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1024, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page);
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "编辑提供商 deepseek" }).click();
  await expect(
    page.getByLabel("API Key 环境变量", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  await expect(page.getByLabel("模型 ID", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("模型名称", { exact: true })).toHaveCount(2);
  await page.screenshot({
    path: testInfo.outputPath("compact-global-models.png"),
    fullPage: true,
  });
  expect(
    await page
      .locator("main")
      .evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
});

test("provider and remote model changes update references; unused registrations can be removed", async ({
  page,
}) => {
  await fakeApi(page);
  let submitted: Record<string, unknown> | undefined;
  let current = globalConfiguration;
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") {
      submitted = route.request().postDataJSON();
      current = {
        ...current, yaml: String(submitted!.yaml),
        effective: JSON.parse(String(submitted!.yaml)), revision: current.revision + 1,
      };
    }
    await route.fulfill({ json: current });
  });
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await expect(
    page.getByRole("button", {
      name: "Delete connection deepseek",
      exact: true,
    }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", {
      name: "Delete model deepseek-flash",
      exact: true,
    }),
  ).toBeDisabled();
  await page.getByLabel("Connection ID", { exact: true }).fill("my_provider");
  await page.getByLabel("Model name", { exact: true }).fill("my-model");
  await page.getByRole("button", { name: "Save provider" }).click();
  await page.getByRole("button", { name: "Edit provider my_provider" }).click();
  await page.getByLabel("Model name", { exact: true }).fill("vendor/final-model:v2");
  await page.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submitted!.model_renames).toEqual({ "my_provider/my-model": "my_provider/vendor/final-model:v2" });
  await page.getByRole("link", { name: "Stage models", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Model: Translate chapters in batches", exact: true })).toHaveText(
    "my_provider / vendor/final-model:v2",
  );
  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await page
    .getByRole("button", { name: "Add provider", exact: true })
    .click();
  await expect(page.getByLabel("Connection ID", { exact: true })).toHaveValue("provider1");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Edit provider my_provider" }).click();
  await page.getByRole("button", { name: "Add model", exact: true }).click();
  await expect(page.getByLabel("Model name", { exact: true })).toHaveCount(2);
  await page
    .getByRole("button", { name: "Delete model", exact: true })
    .click();
  await page.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const saved = JSON.parse(String(submitted!.yaml));
  expect(Object.keys(saved.llm.providers)).toEqual(["my_provider"]);
  expect(saved.llm.models).toEqual({
    "my_provider/vendor/final-model:v2": {},
  });
});

test("duplicate model names cannot overwrite registrations and defaults restore the draft", async ({
  page,
}) => {
  await fakeApi(page);
  let writes = 0;
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") writes++;
    await route.fulfill({ json: globalConfiguration });
  });
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await page.getByRole("button", { name: "Add model", exact: true }).click();
  const newModel = page.getByLabel("Model name", { exact: true }).last();
  await newModel.fill("deepseek-flash");
  await page.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("alert")).toContainText("deepseek/deepseek-flash is already registered.");
  await newModel.fill("");
  await page.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("alert")).toContainText("Enter a remote model name");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page
    .getByRole("button", { name: "Restore defaults", exact: true })
    .click();
  await expect(
    page.getByText("Defaults loaded into the draft. Save to apply.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toBe(0);
  await expect(page.getByRole("button", { name: "Edit provider deepseek" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save configuration", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Global settings saved", { exact: true }),
  ).toBeVisible();
  expect(writes).toBe(1);
});

test("project defaults load without registering models or saving automatically", async ({
  page,
}) => {
  const changed = structuredClone(configuration);
  changed.effective.pipeline.polish = false;
  changed.yaml = JSON.stringify(changed.effective);
  await fakeApi(page, { [`/projects/${pid}/config`]: changed });
  await page.goto(`/projects/${pid}/settings`);
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
  await page
    .getByRole("button", { name: "Restore defaults", exact: true })
    .click();
  await expect(page.getByLabel("Polishing", { exact: true })).toBeChecked();
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
});

test("adding another model preserves a previously saved model rename", async ({
  page,
}) => {
  const registry = globalConfiguration.effective.llm;
  const document = {
    ...globalConfiguration.effective,
    llm: {
      ...registry,
      models: { "deepseek/deepseek-flash": {} },
    },
  };
  let current = {
    ...globalConfiguration,
    effective: document,
    yaml: JSON.stringify(document),
  };
  await fakeApi(page);
  const renames: unknown[] = [];
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      renames.push(body.model_renames);
      current = { ...current, effective: JSON.parse(body.yaml), yaml: body.yaml, revision: current.revision + 1 };
    }
    await route.fulfill({ json: current });
  });
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await page.getByLabel("Model name", { exact: true }).fill("editor");
  await page.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await page.getByRole("button", { name: "Add model", exact: true }).click();
  await page.getByLabel("Model name", { exact: true }).last().fill("writer-v1");
  await page.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(renames).toEqual([{ "deepseek/deepseek-flash": "deepseek/editor" }, {}]);
  expect(Object.keys(current.effective.llm.models)).toEqual(["deepseek/editor", "deepseek/writer-v1"]);
});
