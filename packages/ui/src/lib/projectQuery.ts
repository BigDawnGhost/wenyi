import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isProjectBusy } from "../api/client";
import { platform } from "../platform";

const snapshotKeys = new Set([
  "chapters", "review", "segmentHistory", "precisionDrafts", "terms", "conflicts",
  "subtitles", "workflow", "stats", "review-runs", "review-run", "events",
  "analysis", "preview",
]);

/** Poll only active tasks; reconcile their final saved output before going idle. */
export function useProjectQuery(pid: string, interval = 3000) {
  const qc = useQueryClient();
  const project = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    enabled: !!pid,
    refetchInterval: (query) => isProjectBusy(query.state.data?.status) ? interval : false,
  });
  const status = project.data?.status;
  const previous = useRef({ pid, status });
  useEffect(() => {
    const before = previous.current;
    previous.current = { pid, status };
    if (before.pid !== pid || !isProjectBusy(before.status) || isProjectBusy(status)) return;

    const predicate = (query: { queryKey: readonly unknown[] }) =>
      query.queryKey[1] === pid && snapshotKeys.has(String(query.queryKey[0]));
    const active = qc.getQueryCache().findAll({ type: "active", predicate });
    void qc.invalidateQueries({ predicate, refetchType: "none" });
    const { isForeground } = platform().activity;
    // A pre-hide response may finish in the background. The host reconciles on return.
    if (!isForeground()) return;
    let stopped = false;
    for (const query of active) {
      const wasFetching = query.state.fetchStatus === "fetching";
      const filters = { queryKey: query.queryKey, exact: true, type: "active" as const };
      void qc.refetchQueries(filters, { cancelRefetch: false }).then(() => {
        // An in-flight read may contain the penultimate batch, including on first
        // load. Finish it, then fetch the terminal snapshot without a new timer.
        if (wasFetching && !stopped && isForeground())
          return qc.refetchQueries(filters, { cancelRefetch: false });
      });
    }
    return () => { stopped = true; };
  }, [pid, status, qc]);
  return project;
}
