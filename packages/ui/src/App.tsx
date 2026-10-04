import { Navigate, Route, Routes } from "react-router-dom";
import { Component, lazy, Suspense, type ReactNode } from "react";
import { AppLayout } from "./components/layout/AppLayout";
import { useI18n } from "./i18n";

const Dashboard = lazy(() => import("./features/dashboard/Dashboard"));
const CreateProject = lazy(
  () => import("./features/project-create/CreateProject"),
);
const ProgressPage = lazy(() => import("./features/progress/ProgressPage"));
const GlossaryPage = lazy(() => import("./features/glossary/GlossaryPage"));
const StylePage = lazy(() => import("./features/style/StylePage"));
const ReviewPage = lazy(() => import("./features/review/ReviewPage"));
const ProofreadingPage = lazy(
  () => import("./features/proofreading/ProofreadingPage"),
);
const ExportPage = lazy(() => import("./features/export/ExportPage"));
const EventsPage = lazy(() => import("./features/events/EventsPage"));
const ContentsPage = lazy(() => import("./features/contents/ContentsPage"));
const InterfaceSettingsPage = lazy(
  () => import("./features/settings/InterfaceSettingsPage"),
);
const SettingsPage = lazy(() => import("./features/settings/SettingsPage"));
const SubtitlesPage = lazy(() => import("./features/subtitles/SubtitlesPage"));

class RouteBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? <RouteError /> : this.props.children;
  }
}

function RouteError() {
  const { t } = useI18n();
  return (
    <main role="alert" className="p-8">
      {t("runtime.pageFailed")}{" "}
      <button className="underline" onClick={() => location.reload()}>
        {t("runtime.reload")}
      </button>
    </main>
  );
}

export default function App() {
  return (
    <RouteBoundary>
      <Suspense
        fallback={
          <main aria-busy="true" className="runtime-placeholder" data-route-pending="" />
        }
      >
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/settings" element={<InterfaceSettingsPage />} />
            <Route path="/projects/new" element={<CreateProject />} />
            <Route path="/projects/:pid" element={<ProgressPage />} />
            <Route path="/projects/:pid/glossary" element={<GlossaryPage />} />
            <Route path="/projects/:pid/style" element={<StylePage />} />
            <Route path="/projects/:pid/contents" element={<ContentsPage />} />
            <Route path="/projects/:pid/review" element={<ReviewPage />} />
            <Route
              path="/projects/:pid/proofreading"
              element={<ProofreadingPage />}
            />
            <Route
              path="/projects/:pid/proofreading/:ci"
              element={<ProofreadingPage />}
            />
            <Route path="/projects/:pid/settings" element={<SettingsPage />} />
            <Route path="/projects/:pid/subtitles" element={<SubtitlesPage />} />
            <Route path="/projects/:pid/export" element={<ExportPage />} />
            <Route path="/projects/:pid/events" element={<EventsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </RouteBoundary>
  );
}
