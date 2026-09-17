import type { Page } from "@playwright/test";

export const pid = "book-1";
export const project = {
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
export const chapter = {
  index: 0,
  title: "Chapter One",
  status: "done",
  word_count: 12,
  target_word_count: 12,
  review_issue_count: 0,
  review_status: "pending",
};
export const workflow = {
  source: "snapshot",
  kind: "translation",
  status: "done",
  run_id: "run-a",
  stages: [{ id: "translation", label: "分批翻译章节", enabled: true }],
  progress: null,
};
export const effective = {
  language: { source: "ja", target: "en" },
  llm: {
    preset: "deepseek",
    providers: { default: { kind: "deepseek" } },
    models: { default_model: { provider: "default", model: "deepseek-flash" } },
    tiers: {
      strong: "default_model",
      cheap: "default_model",
      fast: "default_model",
    },
    routes: {},
  },
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
export const projectEffective = {
  ...effective,
  llm: { tiers: effective.llm.tiers, routes: effective.llm.routes, budget: {} },
};
export const configuration = {
  yaml: JSON.stringify(projectEffective, null, 2),
  effective: projectEffective,
  registered_models: effective.llm.models,
  routes: [{ operation: "translate", model: "model-a" }],
  editable: true,
};
export const globalConfiguration = {
  yaml: JSON.stringify(effective, null, 2),
  effective,
  default_template: "标准翻译",
  revision: 0,
};
export const capabilities = {
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
  operations: [
    {
      id: "translation.body",
      tier: "strong",
      description: "Translate paragraphs",
    },
  ],
};

export async function fakeApi(
  page: Page,
  overrides: Record<string, unknown> = {},
) {
  await page.routeWebSocket("**/ws/**", () => {});
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api", "");
    const data: Record<string, unknown> = {
      "/capabilities": capabilities,
      "/settings": globalConfiguration,
      "/settings/defaults": globalConfiguration,
      "/settings/validate": globalConfiguration,
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
      [`/projects/${pid}/config/defaults`]: configuration,
      [`/projects/${pid}/config/validate`]: configuration,
      [`/projects/${pid}/models`]: configuration.routes,
      [`/projects/${pid}/review/runs`]: [],
      [`/projects/${pid}/report`]: {
        summary: { chapters_done: 1, review_issues: 0 },
      },
      [`/projects/${pid}/stats`]: {
        usage: {
          schema_version: 2,
          totals: {
            total_tokens: 100,
            prompt_tokens: 70,
            completion_tokens: 30,
            calls: 1,
          },
          by_model: {},
          by_provider: {},
          by_stage: {},
          by_tier: {},
          labels: {},
        },
        timing: { total_seconds: 12, runs: [] },
      },
      [`/projects/${pid}/workflow`]: workflow,
      [`/projects/${pid}/exports`]: [],
      [`/projects/${pid}/events`]: [],
      [`/projects/${pid}/review/0/segments/0/history`]: [],
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
