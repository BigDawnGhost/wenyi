import { Navigate, Route, Routes } from "react-router-dom";
import {
  Component,
  lazy,
  Suspense,
  useEffect,
  type ReactNode,
} from "react";
import { AppLayout } from "./components/layout/AppLayout";
import { useI18n } from "./i18n";

const loadDashboard = () => import("./features/dashboard/Dashboard");
const loadCreateProject = () => import("./features/project-create/CreateProject");
const loadProgressPage = () => import("./features/progress/ProgressPage");
const loadGlossaryPage = () => import("./features/glossary/GlossaryPage");
const loadStylePage = () => import("./features/style/StylePage");
const loadReviewPage = () => import("./features/review/ReviewPage");
const loadProofreadingPage = () =>
  import("./features/proofreading/ProofreadingPage");
const loadExportPage = () => import("./features/export/ExportPage");
const loadEventsPage = () => import("./features/events/EventsPage");
const loadContentsPage = () => import("./features/contents/ContentsPage");
const loadInterfaceSettingsPage = () =>
  import("./features/settings/InterfaceSettingsPage");
const loadSettingsPage = () => import("./features/settings/SettingsPage");
const loadSubtitlesPage = () => import("./features/subtitles/SubtitlesPage");

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

// Warm every route chunk after the first commit so the first navigation after
// startup or a page refresh does not wait on the network for its module.
const routeLoaders = [
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
  useEffect(() => {
    // A warm-up failure stays quiet: the route retries when it is visited.
    void Promise.all(routeLoaders.map((load) => load())).catch(() => {});
  }, []);
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
