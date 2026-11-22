import type { QueryClient } from "@tanstack/react-query";
import { createElement, Fragment, lazy } from "react";
import type { PlatformServices } from "@wenyi/ui/platform";
import { apiBase, authToken, progressInterval, progressUrl } from "./runtime";
import { request, download } from "./transport";
import { bindSourceDrop } from "./nativeDrop";
import { saveNativeExport } from "./nativeExport";
import { credentialQuery, saveCredential } from "./credentials";
import { activity } from "./activity";
import { library } from "./library";
import { DesktopDiagnostics, DiagnosticExportButton } from "./DesktopDiagnostics";
import { reportDiagnostic } from "./diagnostics";
import { translateDesktop } from "./i18n";
import { NotificationSettings } from "./DesktopNotifications";
import { InterfaceZoomSettings } from "./InterfaceZoomSettings";

const loadDesktopCredential = () => import("./DesktopCredential");
const DesktopCredential = lazy(() =>
  loadDesktopCredential().then((module) => ({ default: module.DesktopCredential })),
);
const DesktopUpdates = lazy(() =>
  import("./DesktopUpdates").then((module) => ({ default: module.DesktopUpdates })),
);
const BabeldocPlugin = lazy(() =>
  import("./BabeldocPlugin").then((module) => ({ default: module.BabeldocPlugin })),
);
// Warm the credential chunk at startup so the settings page never waits on it.
const MinerUCredential = lazy(() =>
  import("./MinerUCredential").then((module) => ({ default: module.MinerUCredential })),
);
void loadDesktopCredential().catch(() => { /* The route retries when opened. */ });

export function desktopPlatform(queryClient: QueryClient): PlatformServices {
  return {
    activity,
    // Only non-sensitive presentation preferences may be persisted.
    preferences: {
      get: (key) => localStorage.getItem(key),
      set: (key, value) => localStorage.setItem(key, value),
      subscribe: (key, listener) => {
        const changed = (event: StorageEvent) => {
          if (event.key === key || event.key === null) listener();
        };
        window.addEventListener("storage", changed);
        return () => window.removeEventListener("storage", changed);
      },
    },
    request,
    download,
    apiBase,
    setAuthToken: () => { /* Native bootstrap exclusively owns the memory-only token. */ },
    connectProgress: (project) => new WebSocket(progressUrl(project)),
    authenticateProgress: (socket) => socket.send(JSON.stringify({ token: authToken() })),
    progressInterval,
    bindSourceDrop,
    capabilities: {
      generalSettings: () => createElement(
        Fragment, null, createElement(InterfaceZoomSettings), createElement(NotificationSettings),
      ),
      diagnostics: {
        label: (locale) => translateDesktop(locale, "diagnostics.title"),
        Section: DesktopDiagnostics,
        ExportButton: DiagnosticExportButton,
        reportRenderFailure: () => reportDiagnostic("frontend_render_failed"),
        reportDiagnostic: () => reportDiagnostic("frontend_message_failed"),
      },
      externalCredentials: { MinerU: MinerUCredential },
      updates: {
        label: (locale) => translateDesktop(locale, "about.title"),
        Section: DesktopUpdates,
      },
      plugins: {
        label: (locale) => translateDesktop(locale, "babeldoc.title"),
        Section: BabeldocPlugin,
      },
      saveExport: saveNativeExport,
      library,
      credentials: {
        Field: DesktopCredential,
        refresh: () => queryClient.refetchQueries({ queryKey: credentialQuery.queryKey }),
        save: (connection, edit) => saveCredential(queryClient, connection, edit),
      },
    },
  };
}
