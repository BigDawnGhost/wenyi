import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import App from "@wenyi/ui/App";
import { DocumentLanguage } from "@wenyi/ui/i18n/DocumentLanguage";
import { configurePlatform } from "@wenyi/ui/platform";
import "@wenyi/ui/index.css";
import { useRuntimeStatus } from "./runtime";
import { useDesktopI18n } from "./i18n";
import { desktopPlatform } from "./platform";
import { configureQueryActivity } from "./activity";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 },
  },
});
configurePlatform(desktopPlatform(queryClient));
configureQueryActivity(queryClient);

function Bootstrap() {
  const status = useRuntimeStatus();
  const { t } = useDesktopI18n();
  if (status === "pending" || status === "closing")
    return (
      <main
        role="status"
        aria-label={t(status === "pending" ? "runtime.starting" : "runtime.closing")}
        aria-busy="true"
        data-runtime-state={status}
        className="runtime-placeholder"
      >
        <svg aria-hidden="true" viewBox="0 0 32 32" className="h-8 w-8 text-muted-foreground">
          <path d="M5 9l5 15 6-12 6 12 5-15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </main>
    );
  if (status === "error")
    return (
      <main role="alert" className="p-8">
        {t("runtime.failed")}: {window.__WENYI_DESKTOP_ERROR__}
        {" "}
        <button className="underline" onClick={() => location.reload()}>
          {t("runtime.reload")}
        </button>
      </main>
    );
  return (
    <>
      <App />
      <Toaster richColors position="top-right" />
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <DocumentLanguage />
        <Bootstrap />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
