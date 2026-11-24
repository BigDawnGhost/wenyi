import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "@wenyi/ui/app";
import { DocumentLanguage } from "@wenyi/ui/document-language";
import { configurePlatform } from "@wenyi/ui/platform";
import { initTheme } from "@wenyi/ui/theme";
import "@wenyi/ui/styles.css";
import { useRuntimeStatus } from "./runtime";
import { useDesktopI18n } from "./i18n";
import { desktopPlatform } from "./platform";
import { configureQueryActivity } from "./activity";
import { installDiagnostics } from "./diagnostics";
import { useNotificationLocale } from "./notifications";
import { DiagnosticExportButton } from "./DesktopDiagnostics";
import { NotificationPermissionPrompt } from "./DesktopNotifications";
import { useInterfaceZoomReset } from "./InterfaceZoomSettings";

installDiagnostics();

// Keep native text-editing menus, but not browser navigation menus in Desktop.
// Bubble normally so React's paragraph context menu can still handle the event.
document.addEventListener("contextmenu", (event) => {
  const target = event.target;
  const textField = (
    target instanceof HTMLTextAreaElement ||
    (target instanceof HTMLInputElement &&
      ["text", "search", "email", "url", "tel", "password", "number"].includes(target.type))
  ) && !target.disabled;
  if (textField || (target instanceof HTMLElement && target.isContentEditable)) return;
  event.preventDefault();
});

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 },
  },
});

// Dev-only offline template: with VITE_MOCK_PLATFORM=1 the shared sandbox
// adapter replaces the native platform wiring so the Desktop shell runs
// offline (docs/development/web.md). Vite replaces both flags statically, so
// production builds prune this branch and keep the native path below.
const mockPlatform =
  import.meta.env.DEV && import.meta.env.VITE_MOCK_PLATFORM === "1";

async function bootstrap() {
  if (mockPlatform) {
    const { installMockPlatform } =
      await import("../../../../packages/ui/sandbox/mock-platform");
    installMockPlatform({ scenario: import.meta.env.VITE_MOCK_SCENARIO });
  } else {
    configurePlatform(desktopPlatform(queryClient));
    configureQueryActivity(queryClient);
  }
  initTheme();
  // The sandbox never sees a native bootstrap event: the runtime status would
  // stay "pending" forever, so mount the app directly under the mock flag.
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <DocumentLanguage />
          {mockPlatform ? <App /> : <Bootstrap />}
        </BrowserRouter>
      </QueryClientProvider>
    </React.StrictMode>,
  );
}

void bootstrap();

function Bootstrap() {
  const status = useRuntimeStatus();
  const { t } = useDesktopI18n();
  useNotificationLocale();
  useInterfaceZoomReset();
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
        <div className="mt-4"><DiagnosticExportButton /></div>
      </main>
    );
  return (
    <>
      <NotificationPermissionPrompt />
      <App />
    </>
  );
}
