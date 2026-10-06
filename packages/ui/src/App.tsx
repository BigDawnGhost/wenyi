import { Navigate, Route, Routes } from "react-router-dom";
import { useEffect } from "react";
import { AppLayout } from "./components/layout/AppLayout";
import { AppErrorBoundary } from "./routes/AppErrorBoundary";
import { pageLoaders, routeEntries, type RouteEntry } from "./routes/manifest";

function renderRoute({ path, element, children }: RouteEntry) {
  return (
    <Route key={path} path={path} element={element}>
      {children?.map(renderRoute)}
    </Route>
  );
}

export default function App() {
  useEffect(() => {
    // A warm-up failure stays quiet: the route retries when it is visited.
    void Promise.all(pageLoaders.map((load) => load())).catch(() => {});
  }, []);
  return (
    <AppErrorBoundary>
      <Routes>
        <Route element={<AppLayout />}>
          {routeEntries.map(renderRoute)}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </AppErrorBoundary>
  );
}
