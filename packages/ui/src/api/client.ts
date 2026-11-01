import type { components } from "@wenyi/shared-schema";
import { platform, type ProjectSource } from "../platform";
import type {
  Output, ProjectInput, ExportOptions, Project, ProjectDetail, ChapterSummary,
  ChapterSegments, SegmentRevision, PrecisionDrafts, Term, TermPage, Conflict,
  StrategyTemplate, ExportOut, EventOut, JobEnqueued, Capabilities, GlobalConfig,
  GlobalConfigInput, ModelPingResult, ModelListResult, ModelListRequest,
  ProjectConfig, ReviewRun, SubtitleData, UploadPreview, AnalysisPayload,
  GlossaryExportFormat,
} from "./types";

const request = <T>(path: string, init?: RequestInit) => platform().request<T>(path, init);
const download = (path: string, fallback: string) => platform().download(path, fallback);
const apiBase = () => platform().apiBase();
export const setAuthToken = (token: string | null) => platform().setAuthToken(token);

// API calls.
export const api = {
  getGlobalDefaults: () => request<GlobalConfig>("/settings/defaults"),
  getProjectDefaults: (pid: string) =>
    request<ProjectConfig>(`/projects/${pid}/config/defaults`),
  getGlobalConfig: () => request<GlobalConfig>("/settings"),
  saveGlobalConfig: (body: GlobalConfigInput) =>
    request<GlobalConfig>("/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  pingModels: (timeout?: number) =>
    request<ModelPingResult[]>("/settings/models/ping", {
      method: "POST",
      body: JSON.stringify(timeout === undefined ? {} : { timeout }),
    }),
  listModels: (body: ModelListRequest) =>
    request<ModelListResult>("/settings/models/list", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  validateGlobalConfig: (body: GlobalConfigInput) =>
    request<GlobalConfig>("/settings/validate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getWorkflow: (pid: string) =>
    request<Output<"WorkflowOut">>(`/projects/${pid}/workflow`),
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
  listProjects: () => request<Project[]>("/projects"),
  createProject: (
    body: ProjectInput,
    file: ProjectSource,
  ) => {
    if ("upload" in file) return file.upload(body);
    const form = new FormData();
    form.append("project", JSON.stringify(body));
    form.append("file", file);
    return request<ProjectDetail>("/projects", {
      method: "POST",
      body: form,
    });
  },
  getProject: (pid: string) => request<ProjectDetail>(`/projects/${pid}`),
  deleteProject: (pid: string) =>
    request<{ message: string }>(`/projects/${pid}`, { method: "DELETE" }),
  translate: (pid: string, strategy?: Record<string, unknown>) =>
    request<JobEnqueued>(`/projects/${pid}/translate`, {
      method: "POST",
      body: JSON.stringify({ strategy }),
    }),
  pause: (pid: string) =>
    request<{ message: string }>(`/projects/${pid}/pause`, { method: "POST" }),
  resume: (pid: string) =>
    request<JobEnqueued>(`/projects/${pid}/resume`, {
      method: "POST",
    }),

  listChapters: (pid: string) =>
    request<ChapterSummary[]>(`/projects/${pid}/chapters`),
  translateChapter: (pid: string, ci: number) =>
    request<JobEnqueued>(`/projects/${pid}/chapters/${ci}/translate`, {
      method: "POST",
    }),

  listTermsPage: (
    pid: string,
    params: { q?: string; type?: string; offset: number; limit: number },
    signal?: AbortSignal,
  ) => {
    const s = new URLSearchParams();
    if (params.q) s.set("q", params.q);
    if (params.type) s.set("type", params.type);
    s.set("offset", String(params.offset));
    s.set("limit", String(params.limit));
    return request<TermPage>(`/projects/${pid}/glossary/terms/page?${s}`, { signal });
  },
  lookupTerms: (pid: string, sources: string[]) =>
    request<Term[]>(`/projects/${pid}/glossary/terms/lookup`, {
      method: "POST",
      body: JSON.stringify({ sources }),
    }),
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
  exportGlossaryUrl: (pid: string, format: GlossaryExportFormat) =>
    `${apiBase()}/projects/${pid}/glossary/export?format=${format}`,
  importGlossary: (pid: string, terms: Partial<Term>[]) =>
    request<{ imported: number }>(`/projects/${pid}/glossary/import`, {
      method: "POST",
      body: JSON.stringify({ terms: terms.map(termInput) }),
    }),

  getReview: (pid: string, ci: number, signal?: AbortSignal) =>
    request<ChapterSegments>(`/projects/${pid}/review/${ci}`, { signal }),
  updateChapterTitle: (
    pid: string,
    ci: number,
    body: components["schemas"]["ChapterTitleUpdate"],
  ) =>
    request<Output<"ChapterTitleOut">>(
      `/projects/${pid}/chapters/${ci}/title`,
      {
        method: "PUT",
        body: JSON.stringify(body),
      },
    ),
  segmentHistory: (pid: string, ci: number, segIdx: number) =>
    request<SegmentRevision[]>(
      `/projects/${pid}/review/${ci}/segments/${segIdx}/history`,
    ),
  precisionDrafts: (pid: string, ci: number, segIdx: number) =>
    request<PrecisionDrafts>(
      `/projects/${pid}/chapters/${ci}/segments/${segIdx}/precision-drafts`,
    ),
  editSegment: (
    pid: string,
    ci: number,
    segIdx: number,
    target: string,
    expectedTarget: string | null,
  ) =>
    request<{ ok: boolean }>(
      `/projects/${pid}/review/${ci}/segments/${segIdx}`,
      {
        method: "PUT",
        body: JSON.stringify({ target, expected_target: expectedTarget }),
      },
    ),
  runAiReview: (pid: string) =>
    request<JobEnqueued>(`/projects/${pid}/review/run`, {
      method: "POST",
      body: JSON.stringify({}),
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
    body: ExportOptions,
  ) =>
    request<Output<"AssembleEnqueued">>(`/projects/${pid}/exports`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  downloadExport: (pid: string, id: number) =>
    download(`/projects/${pid}/exports/${id}/download`, "translation"),
  downloadGlossary: (pid: string, format: GlossaryExportFormat) =>
    download(
      `/projects/${pid}/glossary/export?format=${format}`,
      `glossary.${format}`,
    ),
  downloadExportUrl: (pid: string, id: number) =>
    `${apiBase()}/projects/${pid}/exports/${id}/download`,

  listEvents: (pid: string, type?: string) => {
    const s = new URLSearchParams();
    if (type) s.set("type", type);
    return request<EventOut[]>(`/projects/${pid}/events?${s}`);
  },

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
];
export const isProjectBusy = (status?: string) =>
  ACTIVE_STATUSES.includes(status || "");
