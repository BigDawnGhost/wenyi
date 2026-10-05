import { lazy, type ReactNode } from "react";
import {
  BookOpenCheck,
  Captions,
  FileOutput,
  FolderPlus,
  Languages,
  LayoutDashboard,
  Library,
  ListChecks,
  ListTree,
  ScrollText,
  Settings,
  Settings2,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import type { MessageKey } from "@/i18n";

// Single source of truth for routes, sidebar navigation, chunk warm-up and
// intent prefetch. Adding or removing a page changes only this file.
// Keep routeChunks in packages/ui/tests/platform-boundaries.test.mjs in sync.

const loadDashboard = () => import("../features/dashboard/Dashboard");
const loadCreateProject = () => import("../features/project-create/CreateProject");
const loadProgressPage = () => import("../features/progress/ProgressPage");
const loadGlossaryPage = () => import("../features/glossary/GlossaryPage");
const loadStylePage = () => import("../features/style/StylePage");
const loadReviewPage = () => import("../features/review/ReviewPage");
const loadProofreadingPage = () =>
  import("../features/proofreading/ProofreadingPage");
const loadExportPage = () => import("../features/export/ExportPage");
const loadEventsPage = () => import("../features/events/EventsPage");
const loadContentsPage = () => import("../features/contents/ContentsPage");
const loadInterfaceSettingsPage = () =>
  import("../features/settings/InterfaceSettingsPage");
const loadSettingsPage = () => import("../features/settings/SettingsPage");
const loadSubtitlesPage = () => import("../features/subtitles/SubtitlesPage");

const Dashboard = lazy(loadDashboard);
const CreateProject = lazy(loadCreateProject);
const ProgressPage = lazy(loadProgressPage);
const GlossaryPage = lazy(loadGlossaryPage);
const StylePage = lazy(loadStylePage);
const ReviewPage = lazy(loadReviewPage);
const ProofreadingPage = lazy(loadProofreadingPage);
const ExportPage = lazy(loadExportPage);
const EventsPage = lazy(loadEventsPage);
const ContentsPage = lazy(loadContentsPage);
const InterfaceSettingsPage = lazy(loadInterfaceSettingsPage);
const SettingsPage = lazy(loadSettingsPage);
const SubtitlesPage = lazy(loadSubtitlesPage);

/** One loader per code-split page; warm-up and prefetch iterate this. */
export const pageLoaders: ReadonlyArray<() => Promise<unknown>> = [
  loadDashboard,
  loadCreateProject,
  loadProgressPage,
  loadGlossaryPage,
  loadStylePage,
  loadReviewPage,
  loadProofreadingPage,
  loadExportPage,
  loadEventsPage,
  loadContentsPage,
  loadInterfaceSettingsPage,
  loadSettingsPage,
  loadSubtitlesPage,
];

export interface RouteEntry {
  /** Full path as passed to react-router `<Route path>`. */
  path: string;
  element: ReactNode;
}

export const routeEntries: RouteEntry[] = [
  { path: "/", element: <Dashboard /> },
  { path: "/settings", element: <InterfaceSettingsPage /> },
  { path: "/projects/new", element: <CreateProject /> },
  { path: "/projects/:pid", element: <ProgressPage /> },
  { path: "/projects/:pid/glossary", element: <GlossaryPage /> },
  { path: "/projects/:pid/style", element: <StylePage /> },
  { path: "/projects/:pid/contents", element: <ContentsPage /> },
  { path: "/projects/:pid/review", element: <ReviewPage /> },
  { path: "/projects/:pid/proofreading", element: <ProofreadingPage /> },
  { path: "/projects/:pid/proofreading/:ci", element: <ProofreadingPage /> },
  { path: "/projects/:pid/settings", element: <SettingsPage /> },
  { path: "/projects/:pid/subtitles", element: <SubtitlesPage /> },
  { path: "/projects/:pid/export", element: <ExportPage /> },
  { path: "/projects/:pid/events", element: <EventsPage /> },
];

/** Which projects a nav link applies to, mirroring the historical rules. */
export type Audience = "all" | "book" | "srt";

export function isVisibleFor(audience: Audience, format?: string | null): boolean {
  if (audience === "book") return !!format && format !== "srt";
  if (audience === "srt") return format === "srt";
  return true;
}

export interface NavEntry {
  /** Full path for global entries; ":pid" template for project entries. */
  to: string;
  label: MessageKey;
  icon: LucideIcon;
  /** Exact-match highlighting (overview and the project list). */
  end?: boolean;
  group: "global" | "project";
  /** Sidebar stratum a global entry renders in (layered-sidebar slots). */
  slot?: "action" | "panel" | "foot";
  order: number;
  audience: Audience;
  /** Chunk loader used for warm-up and hover/focus prefetch. */
  loader: () => Promise<unknown>;
}

export const navEntries: NavEntry[] = [
  { to: "/", label: "appLayout.projects", icon: LayoutDashboard, end: true, group: "global", slot: "panel", order: 10, audience: "all", loader: loadDashboard },
  { to: "/projects/new", label: "common.createProject", icon: FolderPlus, group: "global", slot: "action", order: 20, audience: "all", loader: loadCreateProject },
  { to: "/settings", label: "settings.title", icon: Settings, group: "global", slot: "foot", order: 30, audience: "all", loader: loadInterfaceSettingsPage },
  { to: "/projects/:pid", label: "common.translationOverview", icon: Sparkles, end: true, group: "project", order: 10, audience: "all", loader: loadProgressPage },
  { to: "/projects/:pid/proofreading", label: "progress.manualProofreading", icon: BookOpenCheck, group: "project", order: 20, audience: "book", loader: loadProofreadingPage },
  { to: "/projects/:pid/review", label: "common.wholeBookReview", icon: ListChecks, group: "project", order: 30, audience: "book", loader: loadReviewPage },
  { to: "/projects/:pid/subtitles", label: "common.subtitleEditor", icon: Captions, group: "project", order: 40, audience: "srt", loader: loadSubtitlesPage },
  { to: "/projects/:pid/glossary", label: "common.glossary", icon: Library, group: "project", order: 50, audience: "book", loader: loadGlossaryPage },
  { to: "/projects/:pid/style", label: "common.styleSynopsis", icon: Languages, group: "project", order: 60, audience: "book", loader: loadStylePage },
  { to: "/projects/:pid/contents", label: "contents.title", icon: ListTree, group: "project", order: 70, audience: "book", loader: loadContentsPage },
  { to: "/projects/:pid/export", label: "common.export", icon: FileOutput, group: "project", order: 80, audience: "all", loader: loadExportPage },
  { to: "/projects/:pid/settings", label: "common.projectSettings", icon: Settings2, group: "project", order: 90, audience: "all", loader: loadSettingsPage },
  { to: "/projects/:pid/events", label: "common.eventLog", icon: ScrollText, group: "project", order: 100, audience: "all", loader: loadEventsPage },
];

export function navPath(entry: NavEntry, pid?: string): string {
  return entry.to.replace(":pid", pid ?? "");
}
