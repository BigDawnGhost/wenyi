/** Shared progress-frame derivation for the in-page fake socket and the dev
 * server WebSocket simulator. Both speak the backend's frame vocabulary so
 * the live-progress client behaves as it does against the real API. */

import { resolveFixture, runRatio, workflow, type SandboxState } from "./fixtures";

export type ProgressFrame = Record<string, unknown>;

function runId(): string {
  return workflow.run_id ?? "run-a";
}

/** The authenticated snapshot: project and chapter state in one frame. */
export function snapshotFrame(projectId: string, sandbox: SandboxState): ProgressFrame {
  return {
    kind: "snapshot",
    project_id: projectId,
    project: resolveFixture("GET", `/projects/${projectId}`, undefined, {}, sandbox).json,
    chapters: resolveFixture("GET", `/projects/${projectId}/chapters`, undefined, {}, sandbox).json,
  };
}

/** Derive the next progress frames from the sandbox state machine. */
export function tickFrame(
  projectId: string,
  sandbox: SandboxState,
  lastDone: number | null,
): { progress: ProgressFrame | null; terminal: ProgressFrame | null; done: number | null } {
  const ratio = runRatio(sandbox.runs[projectId]);
  if (ratio === null) return { progress: null, terminal: null, done: null };
  const chapters = resolveFixture("GET", `/projects/${projectId}/chapters`, undefined, {}, sandbox)
    .json as { word_count?: number; target_word_count?: number }[];
  const total = chapters.reduce((sum, chapter) => sum + (chapter.word_count || 0), 0);
  const done = chapters.reduce((sum, chapter) => sum + (chapter.target_word_count || 0), 0);
  if (ratio >= 1)
    return { progress: null, terminal: { kind: "state", project_id: projectId, run_id: runId() }, done };
  if (done !== lastDone)
    return {
      progress: {
        kind: "translation",
        project_id: projectId,
        run_id: runId(),
        label: "分批翻译章节",
        done,
        total,
      },
      terminal: null,
      done,
    };
  return { progress: null, terminal: null, done };
}
