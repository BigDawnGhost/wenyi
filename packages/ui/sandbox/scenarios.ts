/** Named offline scenarios for the sandbox fixtures: response overrides,
 * injected faults and runtime seeds. A dev host selects one via a URL
 * parameter or an environment variable (docs/development/web.md). */

import { pid, type SandboxState } from "./fixtures";

export interface FaultRule {
  /** Match this uppercase method only; omit to match every method. */
  method?: string;
  /** Prefix string or regular expression tested against the decoded path. */
  path: string | RegExp;
  /** Omitted kinds are HTTP faults; normalized by matchFault for every host. */
  kind?: "http" | "network" | "delay";
  status?: number;
  body?: unknown;
  detail?: string;
  delayMs?: number;
}

export interface Scenario {
  id: string;
  label: string;
  description: string;
  /** Extra fixture-table entries merged over the base responses. */
  overrides?: Record<string, unknown>;
  /** Faults injected before fixture resolution. */
  faults?: FaultRule[];
  /** Replaces the legacy generic success for unmocked write endpoints. */
  unknownWrite?: "ok" | "notFound";
  /** Adjusts sandbox runtime state when the scenario becomes active. */
  seed?: (state: SandboxState) => void;
}

function startRun(state: SandboxState, elapsedMs = 0): void {
  const run = (state.runs[pid] ??= { startedAt: null, pausedAt: null });
  run.startedAt = Date.now() - elapsedMs;
  run.pausedAt = null;
}

export const scenarios: Record<string, Scenario> = {
  default: {
    id: "default",
    label: "Default",
    description: "Static fixtures with the 15-second translation state machine.",
  },
  empty: {
    id: "empty",
    label: "Empty library",
    description: "No projects; exercise empty states.",
    overrides: { "/projects": [] },
  },
  translating: {
    id: "translating",
    label: "Translation running",
    description: "A run already in flight, so progress frames stream at once.",
    seed: (state) => startRun(state, 4_000),
  },
  paused: {
    id: "paused",
    label: "Translation paused",
    description: "A run paused mid-flight.",
    seed: (state) => {
      startRun(state, 6_000);
      state.runs[pid].pausedAt = Date.now();
    },
  },
  reviewFindings: {
    id: "reviewFindings",
    label: "Review findings",
    description: "Review runs exist for the sample book.",
    overrides: {
      [`/projects/${pid}/review/runs`]: [
        {
          id: "review-1",
          review_id: "review-1",
          status: "done",
          created_at: "2026-10-05T10:00:00Z",
          issues: [],
        },
      ],
    },
  },
  glossaryConflicts: {
    id: "glossaryConflicts",
    label: "Glossary conflicts",
    description: "Terms and unresolved conflicts are present.",
    overrides: {
      [`/projects/${pid}/glossary/terms`]: [
        {
          source: "地脉",
          target: "ley line",
          reading: "ちみゃく",
          type: "term",
          gender: "",
          aliases: [],
          first_chapter: 0,
          note: "",
          status: "ok",
        },
      ],
      [`/projects/${pid}/glossary/conflicts`]: [
        {
          id: 1,
          source: "霧の谷",
          existing_target: "Mist Valley",
          proposed_target: "Valley of Mist",
          chapter: 0,
        },
      ],
    },
  },
  exportReady: {
    id: "exportReady",
    label: "Exports ready",
    description: "Completed exports appear in the history list.",
    overrides: {
      [`/projects/${pid}/exports`]: [
        {
          id: 1,
          project_id: pid,
          format: "epub",
          status: "done",
          path: "/mock/exports/book-1.epub",
          size: 1024,
          created_at: "2026-10-05T11:00:00Z",
        },
      ],
    },
  },
  error500: {
    id: "error500",
    label: "Server errors",
    description: "Project writes fail with HTTP 500.",
    faults: [{ method: "POST", path: "/projects", status: 500, detail: "Injected server failure" }],
  },
  unauthorized: {
    id: "unauthorized",
    label: "Unauthorized",
    description: "Every request fails with HTTP 401.",
    faults: [{ path: "/", status: 401, detail: "unauthorized" }],
  },
  slowNetwork: {
    id: "slowNetwork",
    label: "Slow network",
    description: "Every request waits 1.5s before answering.",
    faults: [{ path: "/", kind: "delay", delayMs: 1_500 }],
  },
  strict: {
    id: "strict",
    label: "Strict endpoints",
    description: "Unmocked write endpoints return 404 instead of a generic success.",
    unknownWrite: "notFound",
  },
};

export function pickScenario(id: string | null | undefined): Scenario {
  return (id !== null && id !== undefined && scenarios[id]) || scenarios.default;
}

export function scenarioFromSearch(search: string): Scenario {
  return pickScenario(new URLSearchParams(search).get("scenario"));
}

export function matchFault(scenario: Scenario, method: string, path: string): FaultRule | null {
  for (const fault of scenario.faults ?? []) {
    if (fault.method && fault.method.toUpperCase() !== method.toUpperCase()) continue;
    const matched =
      typeof fault.path === "string" ? path.startsWith(fault.path) : fault.path.test(path);
    if (matched) return { ...fault, kind: fault.kind ?? "http" };
  }
  return null;
}
