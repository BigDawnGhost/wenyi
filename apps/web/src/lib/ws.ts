import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

export interface ProgressMessage {
  run_id?: string;
  kind: string; // snapshot | stats | state | progress | batch | chapter | term | pipeline | log
  project_id?: string;
  done?: number;
  total?: number;
  label?: string;
  updated_at?: string;
  elapsed_seconds?: number;
  payload?: Record<string, unknown>;
  project?: Record<string, unknown>;
  chapters?: unknown[];
}

export function useProjectProgress(pid: string | undefined) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<ProgressMessage | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setMsg(null);
    setConnected(false);
    if (!pid) return;
    let backoff = 500;
    let stopped = false;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let refresh: ReturnType<typeof setTimeout> | undefined;
    let forceRefresh = false;
    const pending = new Set<string>();
    const invalidate = (keys: string[], force = false) => {
      keys.forEach((key) => pending.add(key));
      forceRefresh ||= force;
      if (refresh !== undefined) return;
      // Coalesce bursts from concurrent batches while retaining the final event.
      refresh = setTimeout(() => {
        refresh = undefined;
        for (const key of pending)
          qc.invalidateQueries(
            { queryKey: [key, pid] },
            { cancelRefetch: forceRefresh },
          );
        pending.clear();
        forceRefresh = false;
      }, 500);
    };
    const all = [
      "project",
      "chapters",
      "subtitles",
      "workflow",
      "stats",
      "report",
      "review-runs",
      "review-run",
      "events",
    ];

    const connect = () => {
      if (stopped) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(
        `${proto}//${location.host}/ws/projects/${pid}/progress`,
      );
      wsRef.current = ws;
      ws.onopen = () => {
        if (stopped) return;
        ws.send(
          JSON.stringify({ token: localStorage.getItem("wenyi_token") || "" }),
        );
        setConnected(true);
        backoff = 500;
        invalidate(all, true);
      };
      ws.onmessage = (ev) => {
        if (stopped || ws !== wsRef.current) return;
        try {
          const incoming = JSON.parse(ev.data) as ProgressMessage;
          if (!incoming || typeof incoming.kind !== "string") return;
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
          } else if (incoming.kind === "state") {
            invalidate(all, true);
          } else {
            setMsg(incoming);
            invalidate([
              "project",
              "chapters",
              "subtitles",
              "workflow",
              "stats",
            ]);
          }
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        if (stopped || ws !== wsRef.current) return;
        setConnected(false);
        setMsg(null);
        if (!stopped) {
          reconnect = setTimeout(connect, Math.min(backoff, 5000));
          backoff *= 2;
        }
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => {
      stopped = true;
      clearTimeout(reconnect);
      clearTimeout(refresh);
      wsRef.current?.close();
    };
  }, [pid, qc]);

  return { msg, connected };
}
