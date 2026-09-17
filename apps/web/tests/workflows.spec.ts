import { test, expect, type Page } from "@playwright/test";

import {
  fakeApi,
  pid,
  project,
  workflow,
  effective,
  configuration,
  capabilities,
  globalConfiguration,
} from "./fixtures";

test("creates a multilingual project and waits for background parsing", async ({
  page,
}, testInfo) => {
  await fakeApi(page, {
    [`/projects/${pid}`]: {
      ...project,
      fmt: "docx",
      status: "uploaded",
      initialized: false,
      source_meta: { original_filename: "test.docx" },
    },
  });
  let created: Record<string, unknown> | undefined;
  let uploaded = false;
  await page.route("**/api/projects", async (r) => {
    const form = await new Response(
      new Uint8Array(r.request().postDataBuffer()!),
      {
        headers: { "content-type": r.request().headers()["content-type"] },
      },
    ).formData();
    created = JSON.parse(String(form.get("project")));
    const source = form.get("file") as File;
    uploaded =
      source.name === "test.docx" && (await source.text()) === "fixture";
    await r.fulfill({
      json: { ...project, fmt: "docx", status: "parsing", initialized: false },
    });
  });
  await page.route(`**/api/projects/${pid}/preview`, async (r) =>
    r.fulfill({
      json: {
        title: "Document",
        fmt: "docx",
        chapter_count: 1,
        total_word_count: 12,
        chapters: [{ index: 0, title: "One", word_count: 12 }],
      },
    }),
  );
  await page.route(`**/api/projects/${pid}/translate`, async (r) =>
    r.fulfill({
      json: { job_id: "translate-1", kind: "translate", project_id: pid },
    }),
  );
  await page.goto("/projects/new");
  await page
    .getByLabel("Project name", { exact: true })
    .fill("Multilingual document");
  await page.getByLabel("Target language", { exact: true }).selectOption("en");
  await expect(page.getByLabel("Translation workflow")).toHaveCount(0);
  const create = page.getByRole("button", {
    name: "Create project",
    exact: true,
  });
  await expect(create).toBeDisabled();
  expect(created).toBeUndefined();
  await expect(
    page.getByRole("checkbox", { name: "Prepare before translating" }),
  ).not.toBeChecked();
  const browseFiles = page.getByRole("button", {
    name: "Browse files",
    exact: true,
  });
  await expect(browseFiles).toBeVisible();
  await expect(
    page.getByText("No file selected", { exact: true }),
  ).toBeVisible();
  await browseFiles
    .locator("..")
    .screenshot({ path: testInfo.outputPath("upload-control-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await browseFiles
    .locator("..")
    .screenshot({ path: testInfo.outputPath("upload-control-mobile.png") });
  await page.setViewportSize({ width: 1280, height: 720 });
  const chooserPromise = page.waitForEvent("filechooser");
  await browseFiles.focus();
  await browseFiles.press("Enter");
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name: "test.docx",
    mimeType:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("fixture"),
  });
  await expect(create).toBeEnabled();
  expect(created).toBeUndefined();
  await create.click();
  await expect(page.getByText("Document", { exact: true })).toBeVisible();
  await expect(page.getByText("test.docx", { exact: true })).toBeVisible();
  await expect(browseFiles).toBeDisabled();
  expect(uploaded).toBe(true);
  expect(created?.target_lang).toBe("en");
  expect(created).not.toHaveProperty("strategy");
  expect(created?.prepare).toBe(false);
  await page
    .getByRole("button", { name: "Start translation", exact: true })
    .click();
  await expect(page).toHaveURL(`/projects/${pid}`);
});

test("unreviewed chapters remain unreviewed and obsolete QA actions are absent", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto(`/projects/${pid}`);
  await expect(page.getByText("Not reviewed", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Consistency check", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("Back-translation findings")).toHaveCount(0);
  await expect(page.getByText("Total usage & run time")).toBeVisible();
});

test("autofix defaults on in project settings and review sends no temporary override", async ({
  page,
}) => {
  await fakeApi(page);
  let saved = { ...effective, pipeline: { ...effective.pipeline } };
  await page.route(`**/api/projects/${pid}/config`, async (r) => {
    if (r.request().method() === "PUT")
      saved = JSON.parse(r.request().postDataJSON().yaml);
    await r.fulfill({
      json: { ...configuration, effective: saved, yaml: JSON.stringify(saved) },
    });
  });
  let request: Record<string, unknown> | undefined;
  await page.route(`**/api/projects/${pid}/review/run`, async (r) => {
    request = r.request().postDataJSON();
    await r.fulfill({
      json: { job_id: "review-1", kind: "review", project_id: pid },
    });
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.getByLabel("Apply autofixes to the saved translation after review"),
  ).toBeChecked();
  await page
    .getByLabel("Apply autofixes to the saved translation after review")
    .uncheck();
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect.poll(() => saved.pipeline.review_autofix).toBe(false);
  await page.reload();
  await expect(
    page.getByLabel("Apply autofixes to the saved translation after review"),
  ).not.toBeChecked();
  await page
    .getByRole("link", { name: "Whole-book review", exact: true })
    .click();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Run whole-book review", exact: true })
    .click();
  await expect.poll(() => request).toEqual({});
});

test("failed manual edit keeps the draft and does not show a success state", async ({
  page,
}) => {
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}/review/0/segments/0`, async (r) =>
    r.fulfill({ status: 409, json: { detail: "项目正在执行任务" } }),
  );
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page
    .getByText("Original translation", { exact: true })
    .click({ button: "right" });
  await page
    .getByRole("menuitem", { name: "Edit translation", exact: true })
    .click();
  await page.getByLabel("Edit translation").fill("Keep this draft");
  await page.getByRole("button", { name: "Save translation" }).click();
  await expect(page.getByRole("alert")).toContainText("409");
  await expect(page.getByLabel("Edit translation")).toHaveValue(
    "Keep this draft",
  );
  await expect(
    page.getByText("Translation saved", { exact: true }),
  ).toHaveCount(0);
});

test("subtitle projects expose timeline and only SRT exports", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, fmt: "srt", status: "translating" },
  });
  await page.goto(`/projects/${pid}/subtitles`);
  await expect(
    page.getByText("#007 · 00:00:01,000 → 00:00:03,000"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "你好", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("link", { name: "Glossary", exact: true }),
  ).toHaveCount(0);
  await page.goto(`/projects/${pid}/export`);
  await expect(
    page.getByRole("button", { name: "SRT", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "EPUB", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Generate export" }),
  ).toBeEnabled();
});

test("advanced configuration reports validation errors", async ({
  page,
}, testInfo) => {
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}/config/validate`, async (r) =>
    r.fulfill({
      status: 422,
      json: { detail: "Unknown pipeline option invalid_option" },
    }),
  );
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.locator("summary").filter({ hasText: "Saved model routes" }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("settings.png"),
    fullPage: true,
  });
  await page
    .locator("summary")
    .filter({ hasText: "Advanced YAML configuration" })
    .click();
  await page
    .getByLabel("Advanced YAML configuration", { exact: true })
    .fill("pipeline:\n  invalid_option: true");
  await page
    .getByRole("button", { name: "Validate configuration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("invalid_option");
});

test("authenticates the progress socket before displaying project events", async ({
  page,
}) => {
  await fakeApi(page);
  await page.addInitScript(() =>
    localStorage.setItem("wenyi_token", "test-auth-token"),
  );
  const messages: unknown[] = [];
  await page.routeWebSocket("**/ws/projects/*/progress", (socket) => {
    socket.onMessage((raw) => {
      const message = JSON.parse(String(raw));
      messages.push(message);
      if (message.token !== "test-auth-token") return;
      socket.send(
        JSON.stringify({
          kind: "progress",
          project_id: pid,
          run_id: workflow.run_id,
          label: "Authenticated event",
          done: 1,
          total: 1,
        }),
      );
    });
  });
  await page.goto(`/projects/${pid}`);
  await expect.poll(() => messages[0]).toEqual({ token: "test-auth-token" });
  await expect(page.getByRole("status")).toContainText("Authenticated event");
  await expect(page.getByRole("status")).toContainText("(1/1)");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("global API provider form saves endpoint, model and tier changes", async ({
  page,
}) => {
  const settings = {
    ...effective,
    llm: {
      preset: "deepseek",
      providers: { default: { kind: "deepseek" } },
      models: {
        default_strong: {
          provider: "default",
          model: "deepseek-chat",
          options: { thinking: true },
        },
      },
      tiers: {
        strong: "default_strong",
        cheap: "default_strong",
        fast: "default_strong",
      },
    },
  };
  await fakeApi(page, {
    "/capabilities": {
      ...capabilities,
      providers: ["deepseek", "openai-compatible"],
    },
    "/settings": {
      ...globalConfiguration,
      effective: settings,
      yaml: JSON.stringify(settings),
    },
  });
  await page.goto("/settings");
  await page
    .locator("summary")
    .filter({ hasText: "API providers & models" })
    .click();
  await page
    .getByLabel("API provider", { exact: true })
    .selectOption("openai-compatible");
  await page.getByLabel("API base URL").fill("https://example.com/v1");
  await page.getByLabel("API key environment variable").fill("CUSTOM_API_KEY");
  await page.getByLabel("Model name", { exact: true }).fill("custom-model");
  const request = page.waitForRequest(
    (r) => r.method() === "PUT" && r.url().endsWith("/settings"),
  );
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  const saved = JSON.parse((await request).postDataJSON().yaml);
  expect(saved.llm.providers.default).toMatchObject({
    kind: "openai-compatible",
    base_url: "https://example.com/v1",
    api_key_env: "CUSTOM_API_KEY",
  });
  expect(saved.llm.models.default_strong.model).toBe("custom-model");
  expect(saved.llm.models.default_strong.options).toEqual({});
  expect(saved.pipeline).toEqual(settings.pipeline);
});

test("workflow snapshot and cached progress survive a page reload", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/workflow`]: {
      source: "snapshot",
      kind: "translation",
      status: "paused",
      run_id: "run-a",
      stages: [
        { id: "translation", label: "分批翻译章节", enabled: true },
        { id: "review", label: "全书审校", enabled: false },
      ],
      progress: {
        label: "正在翻译第 3 章",
        done: 2,
        total: 5,
        run_id: "run-a",
      },
    },
  });
  await page.goto(`/projects/${pid}`);
  await expect(
    page.getByRole("heading", { name: "Current workflow" }),
  ).toBeVisible();
  await expect(
    page.getByText("正在翻译第 3 章", { exact: false }),
  ).toBeVisible();
  await page.getByText("Workflow details", { exact: true }).click();
  await expect(page.getByText("2 · Disabled")).toBeVisible();
  await page.reload();
  await expect(
    page.getByText("正在翻译第 3 章", { exact: false }),
  ).toBeVisible();
});
