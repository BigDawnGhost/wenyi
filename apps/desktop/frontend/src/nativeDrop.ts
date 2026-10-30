import type { SourceDropCallbacks, SourceCapability } from "@wenyi/ui/platform";

export type NativeSource = {
  kind: "native";
  handle: string;
  name: string;
  size: number;
};
export type NativeDrag = {
  kind: "enter" | "over" | "leave" | "drop" | "error";
  position?: { x: number; y: number };
  source?: NativeSource;
  message?: string;
};

declare global {
  interface Window {
    __TAURI_INTERNALS__?: {
      invoke: <T>(command: string, args: Record<string, unknown>) => Promise<T>;
    };
  }
}

function invoke<T>(command: string, args: Record<string, unknown>): Promise<T> {
  if (!window.__TAURI_INTERNALS__)
    return Promise.reject(new Error("Native file selection is unavailable."));
  return window.__TAURI_INTERNALS__.invoke<T>(command, args).catch((error) => {
    throw error instanceof Error ? error : new Error(String(error));
  });
}

function releaseNativeSource(source: NativeSource | null) {
  if (source)
    void invoke("native_drop_release", { handle: source.handle }).catch(() => {});
}

function capability(source: NativeSource): SourceCapability {
  return {
    name: source.name,
    size: source.size,
    upload: (project) => invoke("native_drop_upload", { handle: source.handle, project }),
    release: () => releaseNativeSource(source),
  };
}

export function bindSourceDrop(zone: HTMLElement, callbacks: SourceDropCallbacks) {
  const drag = (event: Event) => {
    const detail = (event as CustomEvent<NativeDrag>).detail;
    const rect = zone.getBoundingClientRect();
    // The native bridge normalizes all platforms to WebView-relative physical pixels.
    const ratio = window.devicePixelRatio || 1;
    const x = (detail.position?.x ?? -1) / ratio;
    const y = (detail.position?.y ?? -1) / ratio;
    const inside = x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
    if (detail.kind === "enter" || detail.kind === "over") {
      callbacks.dragging(!callbacks.disabled && inside);
      return;
    }
    callbacks.dragging(false);
    if (callbacks.disabled) return;
    if (detail.kind === "error" && inside) callbacks.error(detail.message || "");
    if (detail.kind === "drop" && inside && detail.source &&
        callbacks.select(capability(detail.source))) event.preventDefault();
  };
  const preventBrowserDrop = (event: Event) => event.preventDefault();
  window.addEventListener("wenyi:native-drag", drag);
  zone.addEventListener("drop", preventBrowserDrop);
  return () => {
    window.removeEventListener("wenyi:native-drag", drag);
    zone.removeEventListener("drop", preventBrowserDrop);
  };
}

// A drop elsewhere in the app must not leave an unclaimed capability open.
window.addEventListener("wenyi:native-drag", (event) => {
  const detail = (event as CustomEvent<NativeDrag>).detail;
  if (detail?.source)
    queueMicrotask(() => {
      if (!event.defaultPrevented) releaseNativeSource(detail.source!);
    });
});
