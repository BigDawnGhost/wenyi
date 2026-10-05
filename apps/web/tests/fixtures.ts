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
export const preview = {
  title: "Source preview",
  fmt: "epub",
  chapter_count: 3,
  total_word_count: 1200,
  source_lang: "ja",
  chapters: [
    { index: 0, title: "One", word_count: 400 },
    { index: 1, title: "Two", word_count: 400 },
    { index: 2, title: "Three", word_count: 400 },
  ],
};

function tableData(): Record<string, unknown> {
  return {
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
    ],
    "/projects": [project],
    [`/projects/${pid}`]: project,
    [`/projects/${pid}/chapters`]: [chapter],
    [`/projects/${pid}/config`]: configuration,
    [`/projects/${pid}/config/defaults`]: configuration,
    [`/projects/${pid}/config/validate`]: configuration,
    [`/projects/${pid}/models`]: configuration.routes,
    [`/projects/${pid}/preview`]: preview,
    [`/projects/${pid}/glossary/terms`]: [],
    [`/projects/${pid}/glossary/conflicts`]: [],
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
  };
}

export interface FixtureResponse {
  status: number;
  json: unknown;
}

/**
 * Method-aware fixture resolution shared by the Playwright `fakeApi` route and
 * the Vite `MOCK_API` middleware, so automated tests and the manual mock
 * sandbox behave identically:
 *
 * - GET/HEAD: fixture table first, then 404 (tests notice unmocked reads).
 * - Other methods: per-test overrides, then mutation rules (create project,
 *   glossary import), then the table (save/validate respond with stored
 *   state), then a generic success so action buttons can complete.
 */
export function resolveFixture(
  method: string,
  rawPath: string,
  rawBody?: string,
  overrides: Record<string, unknown> = {},
): FixtureResponse {
  const path = rawPath.replace(/^\/api/, "").split("?")[0];
  const verb = method.toUpperCase();
  const data = { ...tableData(), ...overrides };
  if (verb === "GET" || verb === "HEAD") {
    if (path in data) return { status: 200, json: data[path] };
    return { status: 404, json: { detail: `Unexpected endpoint ${path}` } };
  }
  if (path in overrides) return { status: 200, json: overrides[path] };
  if (verb === "POST" && path === "/projects") {
    // Create accepts multipart form data; echo the requested name when present.
    const name = rawBody?.match(/"name"\s*:\s*"([^"]*)"/)?.[1] ?? project.name;
    return { status: 200, json: { ...project, name } };
  }
  if (verb === "POST" && /\/glossary\/import$/.test(path)) {
    return { status: 200, json: { imported: 0 } };
  }
  if (path in data) return { status: 200, json: data[path] };
  const project_id = path.match(/^\/projects\/([^/]+)/)?.[1];
  return {
    status: 200,
    json: { ok: true, message: "mock", job_id: "mock-job", task_id: "mock-job", project_id },
  };
}

export async function fakeApi(
  page: Page,
  overrides: Record<string, unknown> = {},
  apiOrigin?: string,
) {
  await page.routeWebSocket("**/ws/**", () => {});
  await page.route(apiOrigin ? `${apiOrigin}/**` : "**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const rawBody =
      method === "GET" ? undefined : route.request().postData() ?? undefined;
    const { status, json } = resolveFixture(method, pathname, rawBody, overrides);
    return route.fulfill({ status, json });
  });
}
