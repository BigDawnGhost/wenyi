import type { QueryClient } from "@tanstack/react-query";
import { lazy } from "react";
import type { PlatformServices } from "@wenyi/ui/platform";
import { apiBase, authToken, progressInterval, progressUrl } from "./runtime";
import { request, download } from "./transport";
import { bindSourceDrop } from "./nativeDrop";
import { saveNativeExport } from "./nativeExport";
import { credentialQuery } from "./credentials";
import { activity } from "./activity";

const loadDesktopCredential = () => import("./DesktopCredential");
const DesktopCredential = lazy(() =>
  loadDesktopCredential().then((module) => ({ default: module.DesktopCredential })),
);
// Warm the credential chunk at startup so the settings page never waits on it.
void loadDesktopCredential().catch(() => { /* The route retries when opened. */ });

const progressKeys: Record<string, string[]> = {
  progress: ["workflow"],
  pipeline: ["workflow"],
  batch: ["chapters", "subtitles", "stats"],
  chapter: ["project", "chapters", "stats"],
  translation: ["chapters", "workflow"],
  chapter_translation: ["chapters", "workflow"],
  srt: ["subtitles", "workflow"],
  parse: ["project", "chapters", "workflow"],
  prepare: ["project", "chapters", "workflow"],
  review: ["workflow", "review-runs", "review-run"],
  term: ["terms", "conflicts"],
  log: ["events"],
};

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
    progressKeys: (kind) => progressKeys[kind] || ["workflow"],
    bindSourceDrop,
    capabilities: {
      saveExport: saveNativeExport,
      credentials: {
        Field: DesktopCredential,
        refresh: () => queryClient.refetchQueries({ queryKey: credentialQuery.queryKey }),
      },
    },
  };
}
