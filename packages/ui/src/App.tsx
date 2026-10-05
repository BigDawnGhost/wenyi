import { Navigate, Route, Routes } from "react-router-dom";
import { Component, Suspense, useEffect, type ReactNode } from "react";
import { AppLayout } from "./components/layout/AppLayout";
import { useI18n } from "./i18n";
import { pageLoaders, routeEntries } from "./routes/manifest";

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
    void Promise.all(pageLoaders.map((load) => load())).catch(() => {});
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
            {routeEntries.map(({ path, element }) => (
              <Route key={path} path={path} element={element} />
            ))}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </RouteBoundary>
  );
}
