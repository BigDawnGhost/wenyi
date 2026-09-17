import { useEffect, useRef, useState } from "react";

export interface ProgressMessage {
  run_id?: string;
  kind: string; // snapshot | progress | batch | chapter | term | pipeline | log
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
  const [msg, setMsg] = useState<ProgressMessage | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setMsg(null);
    setConnected(false);
    if (!pid) return;
    let backoff = 500;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(
        `${proto}//${location.host}/ws/projects/${pid}/progress`,
      );
      wsRef.current = ws;
      ws.onopen = () => {
        ws.send(
          JSON.stringify({ token: localStorage.getItem("wenyi_token") || "" }),
        );
        setConnected(true);
        backoff = 500;
      };
      ws.onmessage = (ev) => {
        try {
          setMsg(JSON.parse(ev.data) as ProgressMessage);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!stopped) {
          setTimeout(connect, Math.min(backoff, 5000));
          backoff *= 2;
        }
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => {
      stopped = true;
      wsRef.current?.close();
    };
  }, [pid]);

  return { msg, connected };
}
