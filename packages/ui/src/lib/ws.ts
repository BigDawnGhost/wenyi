import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { platform } from "../platform";

export interface ProgressMessage {
  run_id?: string;
  // snapshot | stats | state | progress | batch | chapter | term | pipeline | log
  // | error | heartbeat
  kind: string;
  project_id?: string;
  done?: number;
  total?: number;
  label?: string;
  updated_at?: string;
  elapsed_seconds?: number;
  /** A terminal failure carries the same envelope the HTTP API uses. */
  code?: string;
  message?: string;
  payload?: Record<string, unknown>;
  project?: Record<string, unknown>;
  chapters?: unknown[];
}

/** How long a connection may stay silent before it is treated as dead.
 *
 * The server sends a heartbeat when nothing else is happening, so silence means
 * the socket is gone in a way `onclose` never reported: a dropped network path,
 * a suspended host or a proxy that forgot the connection.
 */
const WATCHDOG_MS = 60_000;

export function useProjectProgress(pid: string | undefined) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<ProgressMessage | null>(null);
  // A terminal failure is not progress: it is kept apart so the stage label in
  // `msg` stays readable while the reason is shown next to it.
  const [failure, setFailure] = useState<ProgressMessage | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setMsg(null);
    setFailure(null);
    setConnected(false);
    if (!pid) return;
    let backoff = 500;
    let stopped = false;
    let lastMessageAt = Date.now();
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let refresh: ReturnType<typeof setTimeout> | undefined;
    let forceRefresh = false;
    const pending = new Set<string>();
    const { isForeground } = platform().activity;
    const invalidate = (keys: string[], force = false) => {
      if (!isForeground()) return;
      keys.forEach((key) => pending.add(key));
      forceRefresh ||= force;
      if (refresh !== undefined) return;
      // Coalesce bursts from concurrent batches while retaining the final event.
      refresh = setTimeout(() => {
        refresh = undefined;
        if (isForeground())
          for (const key of pending)
            qc.invalidateQueries(
              { queryKey: [key, pid] },
              { cancelRefetch: forceRefresh },
            );
        pending.clear();
        forceRefresh = false;
      }, 500);
    };
    const unsubscribeActivity = platform().activity.subscribe(() => {
      if (isForeground()) return;
      // A brief hide/restore must not leave a pre-hide refresh competing with
      // the host's foreground reconciliation.
      clearTimeout(refresh);
      refresh = undefined;
      pending.clear();
      forceRefresh = false;
    });
    const all = [
      "project",
      "chapters",
      "workflow",
      "stats",
      "review-runs",
      "review-run",
      "events",
      "chapterUpdates",
    ];

    const connect = () => {
      if (stopped) return;
      const ws = platform().connectProgress(pid);
      wsRef.current = ws;
      ws.onopen = () => {
        if (stopped) return;
        platform().authenticateProgress(ws);
        setConnected(true);
        // A stale failure belongs to the connection that reported it.
        setMsg(null);
        setFailure(null);
        lastMessageAt = Date.now();
        backoff = 500;
        invalidate(all, true);
      };
      ws.onmessage = (ev) => {
        if (stopped || ws !== wsRef.current) return;
        // Anything at all proves the link is alive, including frames this client
        // filters out below, so liveness is judged before any routing decision.
        lastMessageAt = Date.now();
        // Keep the subscription, but do not render or refetch for background events.
        // The host reconciles saved snapshots when the window becomes foreground.
        if (!isForeground()) return;
        try {
          const incoming = JSON.parse(ev.data) as ProgressMessage;
          if (!incoming || typeof incoming.kind !== "string") return;
          // Only proves liveness; there is no state to show or refetch.
          if (incoming.kind === "heartbeat") return;
          if (incoming.kind === "snapshot") {
            if (incoming.project?.id === pid) invalidate(all, true);
            return;
          }
          if (incoming.project_id !== pid) return;
          const workflow = qc.getQueryData<{ run_id?: string }>([
            "workflow",
            pid,
          ]);
          if (
            !incoming.run_id ||
            (workflow?.run_id && incoming.run_id !== workflow.run_id)
          ) {
            invalidate(["project", "workflow"]);
            return;
          }
          if (incoming.kind === "stats") {
            invalidate(["stats"]);
          } else if (incoming.kind === "error") {
            // A failed run carries no stage label, so keep the last one readable
            // and let the refetch surface the failure instead of "waiting".
            invalidate(["project", "workflow"], true);
            // The frame is the only place the reason survives: the persisted
            // project row keeps a status, not the code the run failed with.
            // It is held beside `msg` rather than replacing it, because the
            // current stage label is the last readable sign of work done and
            // the panel still shows it after the failure.
            setFailure({
              kind: "error",
              project_id: incoming.project_id,
              run_id: incoming.run_id,
              code: incoming.code,
              message: incoming.message,
            });
          } else if (incoming.kind === "state" || incoming.kind === "final") {
            invalidate(all, true);
          } else {
            setMsg(incoming);
            invalidate(platform().progressKeys(incoming.kind));
          }
        } catch (error) {
          // A frame this client cannot read is a defect worth recording, but it
          // must not stop the socket that is still delivering usable frames.
          platform().capabilities.diagnostics?.reportDiagnostic?.("ws.message", error);
        }
      };
      ws.onclose = () => {
        if (stopped || ws !== wsRef.current) return;
        setConnected(false);
        setMsg(null);
        setFailure(null);
        if (!stopped) {
          reconnect = setTimeout(connect, Math.min(backoff, 5000));
          backoff *= 2;
        }
      };
      ws.onerror = () => ws.close();
    };
    connect();
    // Closing is enough: the existing onclose handler already backs off and
    // reconnects, so a dead link recovers without a second retry path.
    const watchdog = setInterval(() => {
      if (stopped || !wsRef.current) return;
      // A background host is expected to be silent, and it reconciles saved
      // state when it returns; reconnecting here would undo that contract.
      if (!isForeground()) {
        lastMessageAt = Date.now();
        return;
      }
      if (Date.now() - lastMessageAt < WATCHDOG_MS) return;
      wsRef.current.close();
    }, WATCHDOG_MS / 2);
    return () => {
      stopped = true;
      unsubscribeActivity();
      clearTimeout(reconnect);
      clearTimeout(refresh);
      clearInterval(watchdog);
      wsRef.current?.close();
    };
  }, [pid, qc]);

  return { msg, failure, connected };
}
