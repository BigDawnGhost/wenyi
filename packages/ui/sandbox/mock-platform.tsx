/** Dev-only PlatformServices implementation backed by the sandbox fixtures.
 * Hosts load it only behind `import.meta.env.DEV && VITE_MOCK_PLATFORM`, so
 * production builds never include it (docs/development/web.md). */

import { configurePlatform, type PlatformServices, type SourceDropCallbacks } from "../src/platform";
import { ApiError } from "../src/lib/http";
import { createSandboxState, resolveFixture, project, type SandboxState } from "./fixtures";
import { snapshotFrame, tickFrame } from "./progress-frames";
import { matchFault, pickScenario, scenarioFromSearch, scenarios, type Scenario } from "./scenarios";

export interface MockPlatformOptions {
  /** Scenario id from `scenarios.ts`; defaults to the `?scenario=` parameter. */
  scenario?: string;
}

const sandbox = createSandboxState();
let activeScenario: Scenario = scenarios.default;
const localeListeners = new Set<() => void>();
const preferences = new Map<string, string>();
let authTokenValue = "";
let sourceDrop: SourceDropCallbacks | null = null;

/** Configure the shared platform singleton with the sandbox adapter. */
export function installMockPlatform(options: MockPlatformOptions = {}): void {
  configurePlatform(createMockPlatform(options));
}

/** Switch scenario at runtime; existing queries are re-read on the next tick. */
export function setScenario(id: string): void {
  activeScenario = pickScenario(id);
  activeScenario.seed?.(sandbox);
}

/** Metadata for a scenario switcher. */
export function listScenarios(): { id: string; label: string; description: string }[] {
  return Object.values(scenarios).map(({ id, label, description }) => ({ id, label, description }));
}

/** Debug hook: simulate a native source drop with a browser File. */
export function triggerMockSourceDrop(file: File): void {
  sourceDrop?.select(file);
}

function fail(response: { status: number; json: unknown }): never {
  const body = (response.json ?? {}) as { detail?: unknown; message?: unknown; code?: unknown };
  const raw = body.detail ?? body.message ?? `HTTP ${response.status}`;
  throw new ApiError({
    status: response.status,
    code: typeof body.code === "string" ? body.code : undefined,
    detail: typeof raw === "string" ? raw : JSON.stringify(raw),
  });
}

/** A WebSocket stand-in whose frames come from the sandbox state machine. */
class FakeWebSocket extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;
  readonly url: string;
  readyState = FakeWebSocket.CONNECTING;
  bufferedAmount = 0;
  extensions = "";
  protocol = "";
  binaryType: BinaryType = "blob";
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  readonly #project: string;
  readonly #sandbox: SandboxState;
  readonly #timers: ReturnType<typeof setTimeout>[] = [];
  #interval: ReturnType<typeof setInterval> | undefined;
  #done: number | null = null;
  #terminalSent = false;
  #lastFrameAt = 0;

  constructor(project: string, sandbox: SandboxState) {
    super();
    this.#project = project;
    this.#sandbox = sandbox;
    this.url = `/ws/projects/${encodeURIComponent(project)}/progress`;
    // A real socket opens asynchronously; keep the same ordering.
    this.#timers.push(
      setTimeout(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.#emit("open", new Event("open"));
      }, 0),
    );
  }

  #emit(type: "open" | "message" | "error" | "close", event: Event): void {
    const handler = `on${type}` as "onopen" | "onmessage" | "onerror" | "onclose";
    (this[handler] as ((event: Event) => void) | null)?.(event);
    this.dispatchEvent(event);
  }

  send(data: string): void {
    if (this.readyState !== FakeWebSocket.OPEN || this.#interval) return;
    try {
      JSON.parse(data) as { token?: string };
    } catch {
      return;
    }
    // The first frame is the authentication message: answer with a snapshot
    // and start streaming state-machine frames, like the backend does.
    this.#deliver(
      new MessageEvent("message", { data: JSON.stringify(snapshotFrame(this.#project, this.#sandbox)) }),
    );
    this.#interval = setInterval(() => this.#tick(), 400);
  }

  #tick(): void {
    if (this.readyState !== FakeWebSocket.OPEN) return;
    const tick = tickFrame(this.#project, this.#sandbox, this.#done);
    this.#done = tick.done;
    if (tick.progress) this.#deliver(new MessageEvent("message", { data: JSON.stringify(tick.progress) }));
    if (tick.terminal && !this.#terminalSent) {
      this.#terminalSent = true;
      this.#deliver(new MessageEvent("message", { data: JSON.stringify(tick.terminal) }));
    }
    if (Date.now() - this.#lastFrameAt >= 30_000)
      this.#deliver(new MessageEvent("message", { data: JSON.stringify({ kind: "heartbeat" }) }));
  }

  #deliver(event: MessageEvent): void {
    this.#lastFrameAt = Date.now();
    this.#emit("message", event);
  }

  close(): void {
    if (this.readyState >= FakeWebSocket.CLOSING) return;
    this.readyState = FakeWebSocket.CLOSED;
    if (this.#interval) clearInterval(this.#interval);
    this.#interval = undefined;
    this.#timers.forEach(clearTimeout);
    this.#timers.length = 0;
    this.#emit("close", new CloseEvent("close"));
  }
}

function MockDiagnosticsSection() {
  return (
    <section className="space-y-2 rounded-lg border bg-card p-4" data-slot="sandbox.diagnostics">
      <h3 className="text-sm font-semibold">Diagnostics</h3>
      <p className="text-sm text-muted-foreground">
        Sandbox diagnostics panel: events stay in memory and never leave the page.
      </p>
    </section>
  );
}

function MockDiagnosticsExport() {
  return (
    <button
      type="button"
      className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
      onClick={() => {
        const blob = new Blob([JSON.stringify({ source: "sandbox", events: [] }, null, 2)], {
          type: "application/json",
        });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "sandbox-diagnostics.json";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1_000);
      }}
    >
      Export diagnostics
    </button>
  );
}

export function createMockPlatform(options: MockPlatformOptions = {}): PlatformServices {
  activeScenario = options.scenario
    ? pickScenario(options.scenario)
    : scenarioFromSearch(typeof location === "undefined" ? "" : location.search);
  activeScenario.seed?.(sandbox);
  return {
    activity: { isForeground: () => true, subscribe: () => () => {} },
    preferences: {
      get: (key) => preferences.get(key) ?? null,
      set: (key, value) => {
        preferences.set(key, value);
        if (key === "wenyi.locale") localeListeners.forEach((listener) => listener());
      },
      subscribe: (_key, listener) => {
        localeListeners.add(listener);
        return () => localeListeners.delete(listener);
      },
    },
    request: async <T,>(path: string, init?: RequestInit): Promise<T> => {
      const method = (init?.method ?? "GET").toUpperCase();
      const body = typeof init?.body === "string" ? init.body : undefined;
      const fault = matchFault(activeScenario, method, path);
      if (fault?.kind === "delay")
        await new Promise((resolve) => setTimeout(resolve, fault.delayMs ?? 1_000));
      if (fault?.kind === "network") throw new TypeError("Failed to fetch (sandbox network fault)");
      const response =
        fault && fault.kind === "http"
          ? { status: fault.status ?? 500, json: fault.body ?? { detail: fault.detail ?? "Injected fault" } }
          : resolveFixture(method, path, body, activeScenario.overrides ?? {}, sandbox, {
              unknownWrite: activeScenario.unknownWrite,
            });
      if (response.status >= 400) fail(response);
      return response.json as T;
    },
    download: async (path, fallback) => {
      const response = resolveFixture("GET", path, undefined, activeScenario.overrides ?? {}, sandbox);
      if (response.status >= 400) fail(response);
      return { path: fallback };
    },
    apiBase: () => "/api",
    setAuthToken: (token) => {
      authTokenValue = token ?? "";
    },
    connectProgress: (project) => new FakeWebSocket(project, sandbox) as unknown as WebSocket,
    authenticateProgress: (socket) => socket.send(JSON.stringify({ token: authTokenValue })),
    progressInterval: (_connected, fallback) => fallback,
    bindSourceDrop: (_zone, callbacks) => {
      sourceDrop = callbacks;
      return () => {
        if (sourceDrop === callbacks) sourceDrop = null;
      };
    },
    capabilities: {
      diagnostics: {
        label: () => "Diagnostics (sandbox)",
        Section: MockDiagnosticsSection,
        ExportButton: MockDiagnosticsExport,
        reportRenderFailure: () => {},
        reportDiagnostic: () => {},
      },
      library: {
        choose: async () => "/mock-library/sandbox-book",
        importProject: async (path) => ({
          ...project,
          id: "sandbox-import",
          name: path.split(/[\\/]/).filter(Boolean).pop() || "Imported book",
          status: "ready",
        }),
        admitLocation: async (path) => path,
      },
    },
  };
}
