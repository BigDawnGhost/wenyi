import { Navigate, Route, Routes } from "react-router-dom";
import { useEffect } from "react";
import { AppLayout } from "./components/layout/AppLayout";
import { AppErrorBoundary } from "./routes/AppErrorBoundary";
import { DesktopOnly } from "./routes/DesktopOnly";
import { pageLoaders, routeEntries, type RouteEntry } from "./routes/manifest";
import { warmPages } from "./routes/warmup";
import { platform } from "./platform";

function renderRoute({ path, element, children }: RouteEntry) {
  return (
    <Route key={path} path={path} element={element}>
      {children?.map(renderRoute)}
    </Route>
  );
}

export default function App() {
  return (
    <AppErrorBoundary>
      <DesktopOnly>
        <Workspace />
      </DesktopOnly>
    </AppErrorBoundary>
  );
}

function Workspace() {
  useEffect(() => warmPages(pageLoaders, platform().activity), []);
  return (
    <Routes>
      <Route element={<AppLayout />}>
        {routeEntries.map(renderRoute)}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
