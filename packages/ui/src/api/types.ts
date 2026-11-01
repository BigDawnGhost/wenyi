import type { components, paths } from "@wenyi/shared-schema";

// FastAPI emits model defaults in responses; request defaults remain optional.
export type Output<Name extends keyof components["schemas"]> = Required<
  components["schemas"][Name]
>;
export type ProjectInput = Pick<components["schemas"]["ProjectCreate"], "name"> &
  Partial<components["schemas"]["ProjectCreate"]>;
export type ExportOptions = Partial<components["schemas"]["ExportRequest"]>;
export type Project = Output<"Project">;
export type ProjectDetail = Output<"ProjectDetail">;
export type ChapterSummary = Output<"ChapterSummary">;
export type ChapterSegments = Output<"ChapterSegments">;
export type SegmentRevision = Output<"SegmentRevision">;
export type PrecisionDrafts = Output<"PrecisionDraftsOut">;
export type Term = Output<"TermOut">;
export type TermPage = Omit<Output<"TermPage">, "items"> & { items: Term[] };
export type GlossaryExportFormat = NonNullable<
  NonNullable<paths["/projects/{pid}/glossary/export"]["get"]["parameters"]["query"]>["format"]
>;
export type Conflict = Output<"ConflictOut">;
export type StrategyTemplate = Output<"StrategyTemplateOut">;
export type ExportFormat = NonNullable<
  components["schemas"]["ExportRequest"]["format"]
>;
export type ExportOut = Output<"ExportOut">;
export type EventOut = Output<"EventOut">;
export type JobEnqueued = Output<"JobEnqueued">;
export type Capabilities = Output<"Capabilities">;
export type GlobalConfig = Output<"GlobalConfigOut">;
export type GlobalConfigInput = components["schemas"]["GlobalConfigInput"];
export type ModelPingResult = Output<"ModelPingResult">;
export type ModelListResult = Output<"ModelListResult">;
export type ModelListRequest =
  paths["/settings/models/list"]["post"]["requestBody"]["content"]["application/json"];
export type ProjectConfig = Output<"ProjectConfigOut">;
export type ReviewRun = Output<"ReviewRun">;
export type ReviewItem = components["schemas"]["ReviewItem"];
export type ReviewLocation = components["schemas"]["ReviewLocation"];
export type Workflow = Output<"WorkflowOut">;
export type SubtitleData = Output<"SubtitleResult">;
export type UploadPreview = Output<"UploadPreview">;
export type AnalysisPayload = Output<"AnalysisOut">;
