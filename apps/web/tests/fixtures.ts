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
  chapter_count: 3,
  total_word_count: 24,
  done_chapters: 3,
  initialized: true,
};
// Specs build their own chapter variants from this base; keep it free of
// presentation-only fields so overrides stay fully deterministic.
export const chapter = {
  index: 0,
  title: "Chapter One",
  status: "done",
  word_count: 12,
  target_word_count: 12,
  review_issue_count: 0,
  review_status: "pending",
};
const chapters = [
  { ...chapter, title_translated: "Chapter One: Departure" },
  {
    index: 1,
    title: "Chapter Two",
    title_translated: "Chapter Two: The Road",
    status: "done",
    word_count: 6,
    target_word_count: 6,
    review_issue_count: 0,
    review_status: "pending",
  },
  {
    index: 2,
    title: "Chapter Three",
    title_translated: "Chapter Three: Arrival",
    status: "done",
    word_count: 6,
    target_word_count: 6,
    review_issue_count: 0,
    review_status: "pending",
  },
];
export const workflow = {
  source: "snapshot",
  kind: "translation",
  status: "done",
  run_id: "run-a",
  stages: [
    { id: "book_understanding", label: "全书理解与结构解析", enabled: true },
    { id: "translation", label: "分批翻译章节", enabled: true },
    { id: "polish", label: "逐章润色", enabled: true },
    { id: "review", label: "全书 Review", enabled: true },
    { id: "review_autofix", label: "Review 自动修复", enabled: true },
    { id: "annotation_alignment", label: "注释与排版对齐", enabled: false },
  ],
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
    [`/projects/${pid}/chapters`]: chapters,
    [`/projects/${pid}/config`]: configuration,
    [`/projects/${pid}/config/defaults`]: configuration,
    [`/projects/${pid}/config/validate`]: configuration,
    [`/projects/${pid}/models`]: configuration.routes,
    [`/projects/${pid}/preview`]: preview,
    [`/projects/${pid}/glossary/terms`]: [],
    [`/projects/${pid}/glossary/conflicts`]: [],
    [`/projects/${pid}/review/runs`]: [],
    [`/projects/${pid}/report`]: {
      summary: { chapters_done: 3, review_issues: 0 },
    },
    [`/projects/${pid}/analysis`]: {
      analysis: {
        genre: "青年向奇幻冒险，轻小说质感",
        tone: "沉静克制，关键冲突处转为激昂",
        narration: "第三人称限知，紧贴主角视角",
        pacing: "前缓后急，每章末尾留钩子",
        register: "口语化书面语，避免文言与过度书面腔",
        dialogue_style: "短句为主，潜台词多，角色语气区分明显",
        rhetoric: "以意象化比喻为主，少用夸张排比",
        style_guide:
          "译文段落以 2–4 句为宜；保留原文的短段落节奏。称呼与专有名词全书统一：\n" +
          "· 「霧の谷」→ Mist Valley（首次出现加注原文）\n" +
          "· 角色台词使用口语缩写，叙述保持中性书面语\n" +
          "· 拟声词按目标语言习惯意译，不逐字照搬",
        book_synopsis:
          "少年遥真在祭典之夜觉醒了听见「地脉呼吸」的能力，被迫离开故乡雾谷，\n" +
          "循着断续的地脉声寻找传说中的沉眠钟楼。旅途中他与失忆的钟守结伴，\n" +
          "逐一平息因地脉紊乱而苏醒的旧物，也逐渐发现自己正是钟楼选中的「敲钟人」。\n" +
          "终章遥真敲响大钟，地脉归位，代价是忘却所有旅伴的名字——\n" +
          "而钟守替他记住了这一切。全书以「失去记忆但留下习惯」的余味收束。",
      },
      chapter_digests: [
        {
          index: 0,
          title: "Chapter One",
          digest:
            "祭典之夜，遥真第一次听见地脉的低鸣，故乡的井水随之干涸。长老暗示他必须离开，母亲偷偷塞给他一枚旧钟舌。",
        },
        {
          index: 1,
          title: "Chapter Two",
          digest:
            "官道上的商队遭「倒行的影子」袭击；遥真用听脉能力找出影子的根，救下商队，也第一次遇见守在废碑旁的失忆钟守。",
        },
        {
          index: 2,
          title: "Chapter Three",
          digest:
            "抵达沉眠钟楼，遥真以钟舌敲钟，地脉归位。代价生效：他忘记旅伴的名字，却在听到钟声时无意识地叫出钟守的名字。",
        },
      ],
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
        by_model: {
          "deepseek-flash": {
            prompt_tokens: 70,
            completion_tokens: 30,
            total_tokens: 100,
            calls: 1,
          },
        },
        by_provider: {
          deepseek: {
            prompt_tokens: 70,
            completion_tokens: 30,
            total_tokens: 100,
            calls: 1,
          },
        },
        by_stage: {
          translation: {
            prompt_tokens: 70,
            completion_tokens: 30,
            total_tokens: 100,
            calls: 1,
          },
        },
        by_tier: {
          strong: {
            prompt_tokens: 70,
            completion_tokens: 30,
            total_tokens: 100,
            calls: 1,
          },
        },
        labels: {
          "deepseek-flash": "Deepseek Flash",
          deepseek: "Deepseek",
          translation: "分批翻译章节",
          strong: "强档模型",
        },
      },
      timing: {
        total_seconds: 12,
        runs: [
          {
            id: "run-3",
            operation: "review",
            status: "done",
            started_at: "2026-10-05T09:40:00Z",
            elapsed_seconds: 1.4,
          },
          {
            id: "run-2",
            operation: "polish",
            status: "done",
            started_at: "2026-10-05T09:20:00Z",
            elapsed_seconds: 3.2,
          },
          {
            id: "run-1",
            operation: "translation",
            status: "done",
            started_at: "2026-10-05T09:00:00Z",
            elapsed_seconds: 7.4,
          },
        ],
      },
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
