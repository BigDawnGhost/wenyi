import { Navigate, Route, Routes } from "react-router-dom";
import { useEffect } from "react";
import { AppLayout } from "./components/layout/AppLayout";
import { AppErrorBoundary } from "./routes/AppErrorBoundary";
import { pageLoaders, routeEntries } from "./routes/manifest";

export default function App() {
  useEffect(() => {
    // A warm-up failure stays quiet: the route retries when it is visited.
    void Promise.all(pageLoaders.map((load) => load())).catch(() => {});
  }, []);
  return (
    <AppErrorBoundary>
      <Routes>
        <Route element={<AppLayout />}>
          {routeEntries.map(({ path, element }) => (
            <Route key={path} path={path} element={element} />
          ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </AppErrorBoundary>
  );
}
