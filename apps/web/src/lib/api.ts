import type { components } from "@wenyi/shared-schema";

// 文译 API 客户端：类型与 OpenAPI 对齐，经 Vite 代理到 :8000（生产由 nginx/api 托管）。

const BASE = "/api";

let authToken: string | null =
  (typeof localStorage !== "undefined" &&
    localStorage.getItem("wenyi_token")) ||
  null;

export function setAuthToken(token: string | null) {
  authToken = token;
  if (typeof localStorage !== "undefined") {
    if (token) localStorage.setItem("wenyi_token", token);
    else localStorage.removeItem("wenyi_token");
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (authToken) headers.set("Authorization", `Bearer ${authToken}`);
  if (
    init?.body &&
    !(init.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      const raw = j.detail || j.message || detail;
      detail = typeof raw === "string" ? raw : JSON.stringify(raw);
    } catch {
      /* noop */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

async function download(path: string, fallback: string) {
  const headers = new Headers();
  if (authToken) headers.set("Authorization", `Bearer ${authToken}`);
  const response = await fetch(`${BASE}${path}`, { headers });
  if (!response.ok)
    throw new Error(`下载失败：${response.status} ${response.statusText}`);
  const disposition = response.headers.get("content-disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const filename = encoded
    ? decodeURIComponent(encoded)
    : disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// FastAPI emits model defaults in responses; request defaults remain optional.
type Output<Name extends keyof components["schemas"]> = Required<
  components["schemas"][Name]
>;
export type Project = Output<"Project">;
export type ProjectDetail = Output<"ProjectDetail">;
export type ChapterSummary = Output<"ChapterSummary">;
export type SegmentOut = Output<"SegmentOut">;
export type ChapterSegments = Output<"ChapterSegments">;
export type Term = Output<"TermOut">;
export type Conflict = Output<"ConflictOut">;
export type StepDef = Output<"StepDef">;
export type StrategyTemplate = Output<"StrategyTemplateOut">;
export type ExportFormat = NonNullable<
  components["schemas"]["ExportRequest"]["format"]
>;
export type PdfEngine = components["schemas"]["ExportRequest"]["pdf_engine"];
export type ExportOut = Output<"ExportOut">;
export type EventOut = Output<"EventOut">;
export type JobEnqueued = Output<"JobEnqueued">;
export type Capabilities = Output<"Capabilities">;
export type ProjectConfig = Output<"ProjectConfigOut">;
export type ReviewRun = Output<"ReviewRun">;
export type SubtitleCue = Output<"SubtitleCue">;
export type SubtitleData = Output<"SubtitleResult">;
export type UploadPreview = Output<"UploadPreview">;
export type AnalysisPayload = Output<"AnalysisOut">;
export interface ReportData {
  summary: Record<string, unknown>;
  usage?: Record<string, unknown>;
  timing?: Record<string, unknown>;
  [key: string]: unknown;
}

// ── 调用 ───────────────────────────────────────────────────────────────
export const api = {
  getWorkflow: (pid: string) => request<Output<"WorkflowOut">>(`/projects/${pid}/workflow`),
  capabilities: () => request<Capabilities>("/capabilities"),
  getPreview: (pid: string) =>
    request<UploadPreview>(`/projects/${pid}/preview`),
  getConfig: (pid: string) => request<ProjectConfig>(`/projects/${pid}/config`),
  saveConfig: (pid: string, yaml: string) =>
    request<ProjectConfig>(`/projects/${pid}/config`, {
      method: "PUT",
      body: JSON.stringify({ yaml }),
    }),
  validateConfig: (pid: string, yaml: string) =>
    request<ProjectConfig>(`/projects/${pid}/config/validate`, {
      method: "POST",
      body: JSON.stringify({ yaml }),
    }),
  modelRoutes: (pid: string) => request<unknown>(`/projects/${pid}/models`),
  checkModels: (
    pid: string,
    workflow: "prepare" | "translate" | "review" | "srt" = "translate",
  ) =>
    request<unknown>(`/projects/${pid}/models/check`, {
      method: "POST",
      body: JSON.stringify({ workflow }),
    }),
  compareModels: (
    pid: string,
    body: Pick<
      components["schemas"]["ModelCompareRequest"],
      "operation" | "models" | "messages"
    > &
      Partial<components["schemas"]["ModelCompareRequest"]>,
  ) =>
    request<JobEnqueued>(`/projects/${pid}/models/compare`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getComparison: (pid: string, jid: string) =>
    request<Record<string, unknown>>(
      `/projects/${pid}/models/comparisons/${encodeURIComponent(jid)}`,
    ),
  getStats: (pid: string) =>
    request<Output<"ProjectStats">>(`/projects/${pid}/stats`),
  listReviewRuns: (pid: string) =>
    request<ReviewRun[]>(`/projects/${pid}/review/runs`),
  getReviewRun: (pid: string, rid: string) =>
    request<ReviewRun>(
      `/projects/${pid}/review/runs/${encodeURIComponent(rid)}`,
    ),
  getSubtitles: (pid: string) =>
    request<SubtitleData>(`/projects/${pid}/subtitles`),
  editSubtitle: (pid: string, id: string, target: string) =>
    request<{ ok: boolean }>(
      `/projects/${pid}/subtitles/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify({ target }) },
    ),
  getReport: (pid: string) => request<ReportData>(`/projects/${pid}/report`),
  listProjects: () => request<Project[]>("/projects"),
  createProject: (
    body: Pick<components["schemas"]["ProjectCreate"], "name"> &
      Partial<components["schemas"]["ProjectCreate"]>,
  ) =>
    request<Project>("/projects", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getProject: (pid: string) => request<ProjectDetail>(`/projects/${pid}`),
  deleteProject: (pid: string) =>
    request<{ message: string }>(`/projects/${pid}`, { method: "DELETE" }),
  uploadSource: (pid: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<JobEnqueued>(`/projects/${pid}/upload`, {
      method: "POST",
      body: fd,
    });
  },
  translate: (pid: string, strategy?: Record<string, unknown>) =>
    request<JobEnqueued>(`/projects/${pid}/translate`, {
      method: "POST",
      body: JSON.stringify({ strategy }),
    }),
  prepare: (pid: string) =>
    request<JobEnqueued>(`/projects/${pid}/prepare`, {
      method: "POST",
    }),
  assemble: (pid: string) =>
    request<Output<"AssembleEnqueued">>(`/projects/${pid}/assemble`, {
      method: "POST",
    }),
  pause: (pid: string) =>
    request<{ message: string }>(`/projects/${pid}/pause`, { method: "POST" }),
  resume: (pid: string) =>
    request<JobEnqueued>(`/projects/${pid}/resume`, {
      method: "POST",
    }),
  regenerateReport: (pid: string) =>
    request<ReportData>(`/projects/${pid}/report`, { method: "POST" }),

  listChapters: (pid: string) =>
    request<ChapterSummary[]>(`/projects/${pid}/chapters`),
  getChapter: (pid: string, ci: number) =>
    request<ChapterSegments>(`/projects/${pid}/chapters/${ci}`),
  translateChapter: (pid: string, ci: number) =>
    request<JobEnqueued>(`/projects/${pid}/chapters/${ci}/translate`, {
      method: "POST",
    }),

  listTerms: (pid: string, params: { q?: string; type?: string } = {}) => {
    const s = new URLSearchParams();
    if (params.q) s.set("q", params.q);
    if (params.type) s.set("type", params.type);
    return request<Term[]>(`/projects/${pid}/glossary/terms?${s}`);
  },
  addTerm: (pid: string, body: Partial<Term>) =>
    request<Term>(`/projects/${pid}/glossary/terms`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateTerm: (pid: string, source: string, body: Partial<Term>) =>
    request<Term>(
      `/projects/${pid}/glossary/terms/${encodeURIComponent(source)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  deleteTerm: (pid: string, source: string) =>
    request<{ message: string }>(
      `/projects/${pid}/glossary/terms/${encodeURIComponent(source)}`,
      { method: "DELETE" },
    ),
  listConflicts: (pid: string) =>
    request<Conflict[]>(`/projects/${pid}/glossary/conflicts`),
  resolveConflict: (
    pid: string,
    cid: number,
    body: { decision: string; target?: string },
  ) =>
    request<{ message: string }>(
      `/projects/${pid}/glossary/conflicts/${cid}/resolve`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  exportGlossaryUrl: (pid: string, format: "json" | "csv") =>
    `${BASE}/projects/${pid}/glossary/export?format=${format}`,
  importGlossary: (pid: string, terms: Partial<Term>[]) =>
    request<{ imported: number }>(`/projects/${pid}/glossary/import`, {
      method: "POST",
      body: JSON.stringify({ terms: terms.map(termInput) }),
    }),

  getReview: (pid: string, ci: number) =>
    request<ChapterSegments>(`/projects/${pid}/review/${ci}`),
  editSegment: (pid: string, ci: number, segIdx: number, target: string) =>
    request<{ ok: boolean }>(
      `/projects/${pid}/review/${ci}/segments/${segIdx}`,
      {
        method: "PUT",
        body: JSON.stringify({ target }),
      },
    ),
  markReviewComplete: (pid: string, ci: number) =>
    request<{ ok: boolean }>(`/projects/${pid}/review/${ci}/complete`, {
      method: "POST",
    }),
  runAiReview: (pid: string, opts?: { autofix?: boolean }) =>
    request<JobEnqueued>(`/projects/${pid}/review/run`, {
      method: "POST",
      body: JSON.stringify(opts ?? {}),
    }),

  getAnalysis: (pid: string) =>
    request<AnalysisPayload>(`/projects/${pid}/analysis`),
  updateDigest: (pid: string, ci: number, digest: string) =>
    request<{ ok: boolean }>(`/projects/${pid}/chapter-digests/${ci}`, {
      method: "PUT",
      body: JSON.stringify({ digest }),
    }),
  updateAnalysis: (pid: string, analysis: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/projects/${pid}/analysis`, {
      method: "PUT",
      body: JSON.stringify({ analysis }),
    }),

  listExports: (pid: string) =>
    request<ExportOut[]>(`/projects/${pid}/exports`),
  createExport: (
    pid: string,
    body: Partial<components["schemas"]["ExportRequest"]>,
  ) =>
    request<Output<"AssembleEnqueued">>(`/projects/${pid}/exports`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  downloadExport: (pid: string, id: number) =>
    download(`/projects/${pid}/exports/${id}/download`, "translation"),
  downloadGlossary: (pid: string, format: "json" | "csv") =>
    download(
      `/projects/${pid}/glossary/export?format=${format}`,
      `glossary.${format}`,
    ),
  downloadExportUrl: (pid: string, id: number) =>
    `${BASE}/projects/${pid}/exports/${id}/download`,

  listEvents: (pid: string, type?: string) => {
    const s = new URLSearchParams();
    if (type) s.set("type", type);
    return request<EventOut[]>(`/projects/${pid}/events?${s}`);
  },

  listSteps: () => request<StepDef[]>("/strategies/steps"),
  listTemplates: () => request<StrategyTemplate[]>("/strategies/templates"),
};

function termInput(term: Partial<Term>) {
  return {
    source: term.source || "",
    target: term.target || "",
    reading: term.reading || "",
    type: term.type || "term",
    gender: term.gender || "",
    aliases: term.aliases || [],
    note: term.note || "",
  };
}

export const ACTIVE_STATUSES = [
  "parsing",
  "queued",
  "preparing",
  "translating",
  "reviewing",
  "autofixing",
  "postprocessing",
  "pausing",
  "comparing",
];
export const isProjectBusy = (status?: string) =>
  ACTIVE_STATUSES.includes(status || "");
export const STATUS_LABELS: Record<string, string> = {
  prepared: "准备完成",
  reviewed: "审校完成",
  comparing: "模型对比中",
  created: "已创建",
  uploaded: "已上传",
  ready: "已就绪",
  queued: "排队中",
  parsing: "解析中",
  preparing: "准备中",
  translating: "翻译中",
  reviewing: "全书审校中",
  autofixing: "自动修复中",
  pausing: "正在保存并暂停",
  paused: "已暂停",
  postprocessing: "译后处理",
  done: "已完成",
  error: "失败",
};
