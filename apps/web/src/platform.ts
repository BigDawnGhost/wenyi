import type { PlatformServices, SourceDropCallbacks } from "@wenyi/ui/platform";
import { decodeResponse, requestHeaders } from "@wenyi/ui/lib/http";
import { translate } from "@wenyi/ui/i18n";

const authToken = () => localStorage.getItem("wenyi_token");

async function download(path: string, fallback: string) {
  const response = await fetch(`/api${path}`, {
    headers: requestHeaders(undefined, authToken()),
  });
  if (!response.ok)
    throw new Error(translate("api.downloadFailed", {
      status: response.status, detail: response.statusText,
    }));
  const disposition = response.headers.get("content-disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded)
    : disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function bindSourceDrop(zone: HTMLElement, callbacks: SourceDropCallbacks) {
  let depth = 0;
  const enter = (event: DragEvent) => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    if (callbacks.disabled) return;
    depth += 1;
    callbacks.dragging(true);
  };
  const over = (event: DragEvent) => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = callbacks.disabled ? "none" : "copy";
  };
  const leave = () => {
    depth = Math.max(0, depth - 1);
    if (!depth) callbacks.dragging(false);
  };
  const drop = (event: DragEvent) => {
    event.preventDefault();
    depth = 0;
    callbacks.dragging(false);
    if (callbacks.disabled) return;
    const files = event.dataTransfer?.files;
    if (files && files.length > 1) callbacks.error(translate("createProject.singleFileOnly"));
    else if (files?.length === 1) callbacks.select(files[0]);
  };
  zone.addEventListener("dragenter", enter);
  zone.addEventListener("dragover", over);
  zone.addEventListener("dragleave", leave);
  zone.addEventListener("drop", drop);
  return () => {
    zone.removeEventListener("dragenter", enter);
    zone.removeEventListener("dragover", over);
    zone.removeEventListener("dragleave", leave);
    zone.removeEventListener("drop", drop);
  };
}

export const webPlatform: PlatformServices = {
  // Preserve browser behavior; native background policy belongs to the Desktop host.
  activity: { isForeground: () => true, subscribe: () => () => {} },
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
  request: async <T>(path: string, init?: RequestInit) =>
    decodeResponse<T>(await fetch(`/api${path}`, {
      ...init, headers: requestHeaders(init, authToken()),
    })),
  download,
  apiBase: () => "/api",
  setAuthToken: (token) => {
    if (token) localStorage.setItem("wenyi_token", token);
    else localStorage.removeItem("wenyi_token");
  },
  connectProgress: (project) => {
    const url = new URL(`/ws/projects/${encodeURIComponent(project)}/progress`, location.origin);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return new WebSocket(url);
  },
  authenticateProgress: (socket) => socket.send(JSON.stringify({ token: authToken() || "" })),
  progressInterval: (_connected, fallback) => fallback,
  progressKeys: () => ["project", "chapters", "subtitles", "workflow", "stats"],
  bindSourceDrop,
  capabilities: {},
};
