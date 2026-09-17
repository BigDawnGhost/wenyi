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
  await expect(
    page.getByLabel("Translate chapters in batches", { exact: true }),
  ).toHaveValue("tier:strong");
  await expect(
    page.getByRole("option", { name: /Follow default tier/ }),
  ).toHaveCount(0);
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
  await expect(page).toHaveURL(new RegExp(`project=${pid}`));
  expect(templateRequests).toEqual([]);
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

test("registry IDs can be renamed with references and new registrations removed", async ({
  page,
}) => {
  await fakeApi(page);
  let submitted: Record<string, unknown> | undefined;
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") {
      submitted = route.request().postDataJSON();
      await route.fulfill({
        json: {
          ...globalConfiguration,
          ...submitted,
          effective: JSON.parse(String(submitted!.yaml)),
          revision: 1,
        },
      });
    } else await route.fulfill({ json: globalConfiguration });
  });
  await page.goto("/settings");
  await page
    .locator("summary")
    .filter({ hasText: "API providers & models" })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Delete connection default",
      exact: true,
    }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", {
      name: "Delete model default_model",
      exact: true,
    }),
  ).toBeDisabled();
  await page.getByLabel("Connection ID", { exact: true }).fill("my_provider");
  await page.getByLabel("Connection ID", { exact: true }).press("Enter");
  await page.getByLabel("Model ID", { exact: true }).fill("my_model");
  await page.getByLabel("Model ID", { exact: true }).press("Enter");
  await page.getByLabel("Model ID", { exact: true }).fill("final_model");
  await page.getByLabel("Model ID", { exact: true }).press("Enter");
  await expect(page.getByLabel("Quality tier", { exact: true })).toHaveValue(
    "final_model",
  );
  await page
    .getByRole("button", { name: "Add API connection", exact: true })
    .click();
  await expect(page.getByLabel("Connection ID", { exact: true })).toHaveCount(
    2,
  );
  await page
    .getByRole("button", { name: "Delete connection provider1", exact: true })
    .click();
  await page.getByRole("button", { name: "Add model", exact: true }).click();
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveCount(2);
  await page
    .getByRole("button", { name: "Delete model model1", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Global settings saved", { exact: true }),
  ).toBeVisible();
  expect(submitted!.model_renames).toEqual({ default_model: "final_model" });
  const saved = JSON.parse(String(submitted!.yaml));
  expect(Object.keys(saved.llm.providers)).toEqual(["my_provider"]);
  expect(saved.llm.models).toEqual({
    final_model: { provider: "my_provider", model: "deepseek-flash" },
  });
});

test("invalid or duplicate IDs cannot overwrite registrations and defaults restore the draft", async ({
  page,
}) => {
  await fakeApi(page);
  let writes = 0;
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") writes++;
    await route.fulfill({ json: globalConfiguration });
  });
  await page.goto("/settings");
  await page
    .locator("summary")
    .filter({ hasText: "API providers & models" })
    .click();
  await page.getByRole("button", { name: "Add model", exact: true }).click();
  const newId = page.getByLabel("Model ID", { exact: true }).last();
  await newId.fill("default_model");
  await newId.press("Enter");
  await expect(page.getByRole("alert")).toHaveText("This ID already exists.");
  await expect(
    page.getByRole("button", { name: "Save configuration", exact: true }),
  ).toBeDisabled();
  await newId.fill("invalid id");
  await newId.press("Enter");
  await expect(page.getByRole("alert")).toContainText("Start with a letter");
  await page
    .getByRole("button", { name: "Restore defaults", exact: true })
    .click();
  await expect(
    page.getByText("Defaults loaded into the draft. Save to apply.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toBe(0);
  await page
    .locator("summary")
    .filter({ hasText: "API providers & models" })
    .click();
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveCount(1);
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

test("adding and renaming another model preserves the original model's rename", async ({
  page,
}) => {
  const registry = globalConfiguration.effective.llm;
  const document = {
    ...globalConfiguration.effective,
    llm: {
      ...registry,
      models: { model1: registry.models.default_model },
      tiers: { strong: "model1", cheap: "model1", fast: "model1" },
    },
  };
  const initial = {
    ...globalConfiguration,
    effective: document,
    yaml: JSON.stringify(document),
  };
  await fakeApi(page);
  let renames = {};
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      renames = body.model_renames;
      await route.fulfill({
        json: {
          ...initial,
          effective: JSON.parse(body.yaml),
          yaml: body.yaml,
          revision: 1,
        },
      });
    } else await route.fulfill({ json: initial });
  });
  await page.goto("/settings");
  await page
    .locator("summary")
    .filter({ hasText: "API providers & models" })
    .click();
  await page.getByLabel("Model ID", { exact: true }).fill("editor");
  await page.getByLabel("Model ID", { exact: true }).press("Enter");
  await page.getByRole("button", { name: "Add model", exact: true }).click();
  await page.getByLabel("Model ID", { exact: true }).last().fill("writer");
  await page.getByLabel("Model ID", { exact: true }).last().press("Enter");
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Global settings saved", { exact: true }),
  ).toBeVisible();
  expect(renames).toEqual({ model1: "editor" });
});
