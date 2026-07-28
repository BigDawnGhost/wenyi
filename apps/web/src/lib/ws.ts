import { useEffect, useRef, useState } from "react";

export interface ProgressMessage {
  kind: string; // snapshot | progress | batch | chapter | term | pipeline | log
  project_id?: string;
  done?: number;
  total?: number;
  label?: string;
  payload?: Record<string, unknown>;
  project?: Record<string, unknown>;
  chapters?: unknown[];
}

export function useProjectProgress(pid: string | undefined) {
  const [msg, setMsg] = useState<ProgressMessage | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!pid) return;
    let backoff = 500;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws/projects/${pid}/progress`);
      wsRef.current = ws;
      ws.onopen = () => {
        setConnected(true);
        backoff = 500;
      };
      ws.onmessage = (ev) => {
        try {
          const data: ProgressMessage = JSON.parse(ev.data);
          setMsg(data);
          if (data.label) {
            setLog((l) => [...l.slice(-300), data.label!]);
          }
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

  return { msg, log, connected };
}
