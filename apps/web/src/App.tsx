import { Routes, Route, Navigate } from "react-router-dom";
import { AppLayout } from "./components/layout/AppLayout";
import Dashboard from "./features/dashboard/Dashboard";
import CreateProject from "./features/project-create/CreateProject";
import ProgressPage from "./features/progress/ProgressPage";
import GlossaryPage from "./features/glossary/GlossaryPage";
import StylePage from "./features/style/StylePage";
import ReviewPage from "./features/review/ReviewPage";
import ExportPage from "./features/export/ExportPage";
import EventsPage from "./features/events/EventsPage";

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/projects/new" element={<CreateProject />} />
        <Route path="/projects/:pid" element={<ProgressPage />} />
        <Route path="/projects/:pid/glossary" element={<GlossaryPage />} />
        <Route path="/projects/:pid/style" element={<StylePage />} />
        <Route path="/projects/:pid/review" element={<ReviewPage />} />
        <Route path="/projects/:pid/review/:ci" element={<ReviewPage />} />
        <Route path="/projects/:pid/export" element={<ExportPage />} />
        <Route path="/projects/:pid/events" element={<EventsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
