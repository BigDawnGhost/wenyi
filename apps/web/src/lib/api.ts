// 文译 API 客户端：类型与 OpenAPI 对齐，经 Vite 代理到 :8000（生产由 nginx/api 托管）。

const BASE = "/api";

let authToken: string | null =
  (typeof localStorage !== "undefined" && localStorage.getItem("wenyi_token")) || null;

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
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || j.message || detail;
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

// ── 类型（对齐 wenyi_api.schemas）──────────────────────────────────────
export interface Project {
  id: string;
  name: string;
  title?: string | null;
  fmt?: string | null;
  source_lang?: string | null;
  target_lang?: string | null;
  status: string;
  strategy?: Record<string, unknown> | null;
  created_at?: string | null;
}
export interface ProjectDetail extends Project {
  book_title?: string | null;
  chapter_count: number;
  total_word_count: number;
  done_chapters: number;
}
export interface UploadPreview {
  title: string;
  fmt: string;
  chapter_count: number;
  total_word_count: number;
  source_lang?: string | null;
  chapters: { index: number; title: string; word_count: number }[];
}
export interface ChapterSummary {
  index: number;
  title: string;
  title_translated?: string | null;
  status: string;
  word_count: number;
  target_word_count: number;
  review_issue_count: number;
}
export interface SegmentOut {
  index: number;
  source: string;
  target?: string | null;
  kind: string;
}
export interface ChapterSegments {
  index: number;
  title: string;
  title_translated?: string | null;
  segments: SegmentOut[];
  review_issues: Record<string, unknown>[];
}
export interface Term {
  source: string;
  target: string;
  reading?: string;
  type?: string;
  gender?: string;
  aliases?: string[];
  first_chapter?: number | null;
  note?: string;
  confidence?: string;
  locked?: boolean;
  status?: string;
}
export interface Conflict {
  id: number;
  source: string;
  existing_target?: string | null;
  proposed_target?: string | null;
  chapter?: number | null;
  note?: string | null;
}
export interface StepDef {
  id: string;
  name: string;
  category: string;
  always_on?: boolean;
  locked?: boolean;
  group?: string | null;
  depends_on?: string[];
  description?: string | null;
  output?: string | null;
  options?: Record<string, unknown> | null;
}
export interface StrategyTemplate {
  name: string;
  description: string;
  time_factor: number;
  recommended?: boolean;
  steps: Record<string, unknown>;
}
export interface ExportOut {
  id: number;
  project_id: string;
  format: string;
  status: string;
  path?: string | null;
  size?: number | null;
  created_at?: string | null;
}
export interface EventOut {
  id: number;
  type: string;
  payload: Record<string, unknown>;
  created_at?: string | null;
}
export interface AnalysisPayload {
  analysis: Record<string, unknown>;
  chapter_digests: { index: number; title: string; digest: string }[];
}

// ── 调用 ───────────────────────────────────────────────────────────────
export const api = {
  listProjects: () => request<Project[]>("/projects"),
  createProject: (body: { name: string; source_lang?: string; target_lang?: string; strategy?: Record<string, unknown> }) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),
  getProject: (pid: string) => request<ProjectDetail>(`/projects/${pid}`),
  deleteProject: (pid: string) => request<{ message: string }>(`/projects/${pid}`, { method: "DELETE" }),
  uploadSource: (pid: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<UploadPreview>(`/projects/${pid}/upload`, { method: "POST", body: fd });
  },
  translate: (pid: string, strategy?: Record<string, unknown>) =>
    request<{ job_id: string; kind: string }>(`/projects/${pid}/translate`, {
      method: "POST",
      body: JSON.stringify({ strategy }),
    }),
  pause: (pid: string) => request<{ message: string }>(`/projects/${pid}/pause`, { method: "POST" }),
  resume: (pid: string) => request<{ job_id: string; kind: string }>(`/projects/${pid}/resume`, { method: "POST" }),

  listChapters: (pid: string) => request<ChapterSummary[]>(`/projects/${pid}/chapters`),
  getChapter: (pid: string, ci: number) => request<ChapterSegments>(`/projects/${pid}/chapters/${ci}`),

  listTerms: (pid: string, params: { q?: string; type?: string; locked?: boolean } = {}) => {
    const s = new URLSearchParams();
    if (params.q) s.set("q", params.q);
    if (params.type) s.set("type", params.type);
    if (params.locked !== undefined) s.set("locked", String(params.locked));
    return request<Term[]>(`/projects/${pid}/glossary/terms?${s}`);
  },
  addTerm: (pid: string, body: Partial<Term>) =>
    request<Term>(`/projects/${pid}/glossary/terms`, { method: "POST", body: JSON.stringify(body) }),
  updateTerm: (pid: string, source: string, body: Partial<Term>) =>
    request<Term>(`/projects/${pid}/glossary/terms/${encodeURIComponent(source)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteTerm: (pid: string, source: string) =>
    request<{ message: string }>(`/projects/${pid}/glossary/terms/${encodeURIComponent(source)}`, { method: "DELETE" }),
  lockTerm: (pid: string, source: string, target?: string) =>
    request<{ message: string }>(`/projects/${pid}/glossary/terms/${encodeURIComponent(source)}/lock`, {
      method: "POST",
      body: JSON.stringify(target ? { target } : {}),
    }),
  unlockTerm: (pid: string, source: string) =>
    request<{ message: string }>(`/projects/${pid}/glossary/terms/${encodeURIComponent(source)}/unlock`, { method: "POST" }),
  listConflicts: (pid: string) => request<Conflict[]>(`/projects/${pid}/glossary/conflicts`),
  resolveConflict: (pid: string, cid: number, body: { decision: string; target?: string }) =>
    request<{ message: string }>(`/projects/${pid}/glossary/conflicts/${cid}/resolve`, { method: "POST", body: JSON.stringify(body) }),

  getReview: (pid: string, ci: number) => request<ChapterSegments>(`/projects/${pid}/review/${ci}`),
  editSegment: (pid: string, ci: number, segIdx: number, target: string) =>
    request<{ ok: boolean }>(`/projects/${pid}/review/${ci}/segments/${segIdx}`, {
      method: "PUT",
      body: JSON.stringify({ target }),
    }),
  markReviewComplete: (pid: string, ci: number) =>
    request<{ ok: boolean }>(`/projects/${pid}/review/${ci}/complete`, { method: "POST" }),

  getAnalysis: (pid: string) => request<AnalysisPayload>(`/projects/${pid}/analysis`),
  updateAnalysis: (pid: string, analysis: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/projects/${pid}/analysis`, { method: "PUT", body: JSON.stringify({ analysis }) }),

  listExports: (pid: string) => request<ExportOut[]>(`/projects/${pid}/exports`),
  createExport: (pid: string, body: { format: string; bilingual?: boolean; order?: string; about_page?: boolean }) =>
    request<{ job_id: string; kind: string }>(`/projects/${pid}/exports`, { method: "POST", body: JSON.stringify(body) }),
  downloadExportUrl: (pid: string, id: number) => `${BASE}/projects/${pid}/exports/${id}/download`,

  listEvents: (pid: string, type?: string) => {
    const s = new URLSearchParams();
    if (type) s.set("type", type);
    return request<EventOut[]>(`/projects/${pid}/events?${s}`);
  },

  listSteps: () => request<StepDef[]>("/strategies/steps"),
  listTemplates: () => request<StrategyTemplate[]>("/strategies/templates"),
};
