import { useSyncExternalStore } from "react";

declare global {
  interface Window {
    __WENYI_DESKTOP_PENDING__?: boolean;
    __WENYI_DESKTOP__?: { apiBase: string; token: string };
    __WENYI_DESKTOP_STATUS__?: "error" | "closing";
    __WENYI_DESKTOP_ERROR__?: string;
    __WENYI_DESKTOP_CLOSING__?: boolean;
    __WENYI_DESKTOP_BACKGROUND__?: boolean;
  }
}

export type RuntimeStatus = "pending" | "ready" | "error" | "closing";

export function runtimeStatus(): RuntimeStatus {
  if (window.__WENYI_DESKTOP_CLOSING__) return "closing";
  if (window.__WENYI_DESKTOP_ERROR__) return "error";
  if (window.__WENYI_DESKTOP_STATUS__) return window.__WENYI_DESKTOP_STATUS__;
  if (window.__WENYI_DESKTOP__) return "ready";
  return "pending";
}

const listeners = new Set<() => void>();
for (const kind of ["ready", "error", "closing"] as const) {
  window.addEventListener(`wenyi:desktop-${kind}`, (event) => {
    if (kind !== "ready") {
      window.__WENYI_DESKTOP_STATUS__ = kind;
      if (kind === "error")
        window.__WENYI_DESKTOP_ERROR__ = String(
          (event as CustomEvent).detail || "Local service failed.",
        );
    }
    listeners.forEach((listener) => listener());
  });
}

export function useRuntimeStatus() {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    runtimeStatus,
  );
}

function desktopConfig() {
  if (runtimeStatus() !== "ready")
    throw new Error("Local service is unavailable.");
  return window.__WENYI_DESKTOP__!;
}

export function apiBase() {
  return desktopConfig().apiBase.replace(/\/$/, "");
}

export function authToken() {
  return desktopConfig().token;
}

export function progressUrl(pid: string) {
  const origin = desktopConfig().apiBase;
  const url = new URL(`/ws/projects/${encodeURIComponent(pid)}/progress`, origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export function progressInterval(connected: boolean, fallback: number) {
  return connected ? 30_000 : fallback;
}
