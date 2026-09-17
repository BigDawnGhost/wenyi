import { test, expect, type Page } from "@playwright/test";

const pid = "book-1";
const project = {
  id: pid,
  name: "Test Book",
  title: "原文",
  fmt: "epub",
  source_lang: "ja",
  target_lang: "en",
  status: "done",
  chapter_count: 1,
  total_word_count: 12,
  done_chapters: 1,
  initialized: true,
};
const chapter = {
  index: 0,
  title: "Chapter One",
  status: "done",
  word_count: 12,
  target_word_count: 15,
  review_issue_count: 0,
  review_status: "pending",
};
const workflow = {
  source: "snapshot",
  kind: "translation",
  status: "done",
  run_id: "run-a",
  stages: [{ id: "translation", label: "分批翻译章节", enabled: true }],
  progress: null,
};
const effective = {
  language: { source: "ja", target: "en" },
  llm: { preset: "deepseek" },
  segment: { max_tokens_per_batch: 1800, max_tokens_per_segment: 1200 },
  pipeline: {
    book_understanding: true,
    polish: true,
    review: true,
    review_autofix: true,
    annotation_alignment: true,
    review_concurrency: 4,
    pdf_backend: "mineru",
  },
  output: { punctuation_normalize: true },
};
const configuration = {
  yaml: JSON.stringify(effective, null, 2),
  effective,
  routes: [{ operation: "translate", model: "model-a" }],
  editable: true,
};
const capabilities = {
  languages: [
    { code: "zh", name: "中文" },
    { code: "en", name: "英语" },
    { code: "ja", name: "日语" },
  ],
  input_formats: ["epub", "docx", "srt", "pdf"],
  output_formats: ["epub", "txt", "html", "markdown", "docx", "pdf"],
  pdf: {
    backends: ["mineru", "babeldoc"],
    export_backends: ["weasyprint", "fpdf2"],
  },
  providers: ["deepseek"],
  operations: [{ id: "translation.body", description: "Translate paragraphs" }],
};

async function fakeApi(page: Page, overrides: Record<string, unknown> = {}) {
  await page.routeWebSocket("**/ws/**", () => {});
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api", "");
    const data: Record<string, unknown> = {
      "/capabilities": capabilities,
      "/strategies/templates": [
        {
          name: "标准翻译",
          description: "完整流程",
          recommended: true,
          steps: {},
        },
        { name: "快速出稿", description: "初稿", steps: {} },
      ],
      "/projects": [project],
      [`/projects/${pid}`]: project,
      [`/projects/${pid}/chapters`]: [chapter],
      [`/projects/${pid}/config`]: configuration,
      [`/projects/${pid}/config/validate`]: configuration,
      [`/projects/${pid}/models`]: configuration.routes,
      [`/projects/${pid}/review/runs`]: [],
      [`/projects/${pid}/report`]: {
        summary: { chapters_done: 1, review_issues: 0 },
      },
      [`/projects/${pid}/stats`]: {
        usage: { total_tokens: 100 },
        timing: { elapsed_seconds: 12 },
      },
      [`/projects/${pid}/workflow`]: workflow,
      [`/projects/${pid}/exports`]: [],
      [`/projects/${pid}/events`]: [],
      [`/projects/${pid}/review/0`]: {
        index: 0,
        title: "Chapter One",
        segments: [
          {
            index: 0,
            source: "原文第一段",
            target: "Original translation",
            kind: "text",
          },
        ],
        review_issues: [],
      },
      [`/projects/${pid}/subtitles`]: {
        cues: [
          {
            id: "007",
            index: 0,
            start: "00:00:01,000",
            end: "00:00:03,000",
            timestamp: "00:00:01,000 --> 00:00:03,000",
            source: "Hello",
            target: "你好",
            status: "done",
          },
        ],
        completed: 1,
        total: 1,
      },
      ...overrides,
    };
    if (path in data) return route.fulfill({ json: data[path] });
    return route.fulfill({
      status: 404,
      json: { detail: `Unexpected endpoint ${path}` },
    });
  });
}

test("creates a multilingual project and waits for background parsing", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}`]: {
      ...project,
      fmt: null,
      status: "created",
      initialized: false,
    },
  });
  let created: Record<string, unknown> | undefined;
  let uploaded = false;
  await page.route("**/api/projects", async (r) => {
    created = r.request().postDataJSON();
    await r.fulfill({
      json: { ...project, status: "created", initialized: false },
    });
  });
  await page.route(`**/api/projects/${pid}/upload`, async (r) => {
    uploaded = true;
    await r.fulfill({
      json: { job_id: "parse-1", project_id: pid, kind: "parse" },
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
    .getByLabel("项目名称", { exact: true })
    .fill("Multilingual document");
  await page.getByLabel("目标语言", { exact: true }).selectOption("en");
  await expect(page.getByLabel("翻译流程")).toHaveValue("标准翻译");
  await page.getByRole("button", { name: "创建并配置" }).click();
  await expect(page.getByLabel("上传原文")).toBeVisible();
  expect(created?.target_lang).toBe("en");
  expect(created?.strategy).toEqual({ template: "标准翻译" });
  await page.getByLabel("上传原文").setInputFiles({
    name: "test.docx",
    mimeType:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("fixture"),
  });
  await expect(page.getByText("Document", { exact: true })).toBeVisible();
  expect(uploaded).toBe(true);
  await page.getByRole("button", { name: "开始翻译", exact: true }).click();
  await expect(page).toHaveURL(`/projects/${pid}`);
});

test("unreviewed chapters remain unreviewed and obsolete QA actions are absent", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto(`/projects/${pid}`);
  await expect(page.getByText("未审校", { exact: true })).toBeVisible();
  await expect(page.getByText("一致性检查", { exact: true })).toHaveCount(0);
  await expect(page.getByText("回译疑点")).toHaveCount(0);
  await expect(page.getByText("累计用量与运行耗时")).toBeVisible();
});

test("review defaults to configured autofix and sends no force flag", async ({
  page,
}) => {
  await fakeApi(page);
  let request: Record<string, unknown> | undefined;
  await page.route(`**/api/projects/${pid}/review/run`, async (r) => {
    request = r.request().postDataJSON();
    await r.fulfill({
      json: { job_id: "review-1", kind: "review", project_id: pid },
    });
  });
  await page.goto(`/projects/${pid}/review`);
  await expect(page.getByLabel("审校后自动修复并写回正式译文")).toBeChecked();
  await page.getByLabel("审校后自动修复并写回正式译文").uncheck();
  await page.getByRole("button", { name: "运行全书审校", exact: true }).click();
  await expect.poll(() => request).toEqual({ autofix: false });
});

test("failed manual edit keeps the draft and does not show a success state", async ({
  page,
}) => {
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}/review/0/segments/0`, async (r) =>
    r.fulfill({ status: 409, json: { detail: "项目正在执行任务" } }),
  );
  await page.goto(`/projects/${pid}/review/0`);
  await page.getByRole("button", { name: "Original translation" }).click();
  await page.getByLabel("编辑译文").fill("Keep this draft");
  await page.getByRole("button", { name: "保存译文" }).click();
  await expect(page.getByRole("alert")).toContainText("409");
  await expect(page.getByLabel("编辑译文")).toHaveValue("Keep this draft");
  await expect(page.getByText("译文已保存", { exact: true })).toHaveCount(0);
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
    page.getByRole("link", { name: "术语表", exact: true }),
  ).toHaveCount(0);
  await page.goto(`/projects/${pid}/export`);
  await expect(
    page.getByRole("button", { name: "SRT", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "EPUB", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "生成导出文件" }),
  ).toBeEnabled();
});

test("advanced configuration reports validation errors and comparison is explicit", async ({ page }, testInfo) => {
  await fakeApi(page);
  let compares = 0;
  await page.route(`**/api/projects/${pid}/config/validate`, async (r) =>
    r.fulfill({
      status: 422,
      json: { detail: "Unknown pipeline option invalid_option" },
    }),
  );
  await page.route(`**/api/projects/${pid}/models/compare`, async (r) => {
    compares++;
    await r.fulfill({
      json: { job_id: "comparison-1", kind: "models_compare", project_id: pid },
    });
  });
  await page.route(
    `**/api/projects/${pid}/models/comparisons/comparison-1`,
    async (r) =>
      r.fulfill({
        json: {
          status: "completed",
          results: [
            {
              model: "model-a",
              output: "Hello",
              total_tokens: 12,
              elapsed_seconds: 1,
            },
          ],
        },
      }),
  );
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.getByText("已保存配置的模型路由", { exact: true }),
  ).toBeVisible();
  expect(compares).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("settings.png"), fullPage: true });
  await page.getByText("高级 YAML 配置", { exact: true }).click();
  await page
    .getByLabel("高级 YAML 配置", { exact: true })
    .fill("pipeline:\n  invalid_option: true");
  await page.getByRole("button", { name: "校验配置", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("invalid_option");
  await page.getByLabel("模型 ID（逗号分隔）").fill("model-a");
  await page.getByLabel("模型对比测试消息").fill("Say hello");
  await page.getByRole("button", { name: "运行模型对比", exact: true }).click();
  await expect(page.getByText("Hello", { exact: true })).toBeVisible();
  expect(compares).toBe(1);
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
  await expect(page.getByRole("status")).toContainText("（1/1）");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("API provider form saves endpoint, model and tier changes", async ({ page }) => {
  const settings = { ...effective, llm: { preset: "deepseek", providers: { default: { kind: "deepseek" } }, models: { default_strong: { provider: "default", model: "deepseek-chat", options: { thinking: true } } }, tiers: { strong: "default_strong", cheap: "default_strong", fast: "default_strong" } } };
  await fakeApi(page, {
    "/capabilities": { ...capabilities, providers: ["deepseek", "openai-compatible"] },
    [`/projects/${pid}/config`]: { ...configuration, effective: settings, yaml: JSON.stringify(settings) },
  });
  await page.goto(`/projects/${pid}/settings`);
  await page.getByLabel("API 供应商", { exact: true }).selectOption("openai-compatible");
  await page.getByLabel("API 地址（Base URL）").fill("https://example.com/v1");
  await page.getByLabel("API Key 环境变量").fill("CUSTOM_API_KEY");
  await page.getByLabel("模型名称", { exact: true }).fill("custom-model");
  const request = page.waitForRequest(r => r.method() === "PUT" && r.url().endsWith(`/projects/${pid}/config`));
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  const saved = JSON.parse((await request).postDataJSON().yaml);
  expect(saved.llm.providers.default).toMatchObject({ kind: "openai-compatible", base_url: "https://example.com/v1", api_key_env: "CUSTOM_API_KEY" });
  expect(saved.llm.models.default_strong.model).toBe("custom-model");
  expect(saved.llm.models.default_strong.options).toEqual({});
  expect(saved.pipeline).toEqual(settings.pipeline);
});

test("workflow snapshot and cached progress survive a page reload", async ({ page }) => {
  await fakeApi(page, { [`/projects/${pid}/workflow`]: {
    source: "snapshot", kind: "translation", status: "paused", run_id: "run-a",
    stages: [{ id: "translation", label: "分批翻译章节", enabled: true }, { id: "review", label: "全书审校", enabled: false }],
    progress: { label: "正在翻译第 3 章", done: 2, total: 5, run_id: "run-a" },
  } });
  await page.goto(`/projects/${pid}`);
  await expect(page.getByRole("heading", { name: "当前翻译流程" })).toBeVisible();
  await expect(page.getByText("正在翻译第 3 章", { exact: false })).toBeVisible();
  await expect(page.getByText("2 · 已关闭")).toBeVisible();
  await page.reload();
  await expect(page.getByText("正在翻译第 3 章", { exact: false })).toBeVisible();
});
