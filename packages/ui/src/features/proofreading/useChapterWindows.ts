import { useCallback, useRef } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { ChapterWindow, ReaderSegment } from "../../api/types";

export const WINDOW_SIZE = 32;
export const windowKey = (pid: string, ci: number, sourceOnly: boolean) =>
  ["chapterWindow", pid, ci, sourceOnly] as const;

/** Keep only the current windows. Refresh targets conditionally, never their sources. */
export function useChapterWindows(
  pid: string,
  ci: number,
  sourceOnly: boolean,
  busy: boolean,
  initial: ChapterWindow,
  positions: number[],
  pinned: ReaderSegment | undefined,
) {
  const qc = useQueryClient();
  const key = windowKey(pid, ci, sourceOnly);
  const visible = [...new Set(positions.map((p) => Math.floor(p / WINDOW_SIZE) * WINDOW_SIZE))];
  // Request the viewport first, then one block on either side. A moving viewport
  // cancels abandoned queries instead of queueing every block crossed while dragging.
  const offsets = [...new Set([...visible, ...visible.flatMap((offset) =>
    [offset + WINDOW_SIZE, offset - WINDOW_SIZE])])]
    .filter((offset) => offset !== initial.offset && offset >= 0 && offset < initial.total);
  const queries = useQueries({
    queries: offsets.map((offset) => ({
      queryKey: [...key, offset],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        api.chapterWindow(pid, ci, offset, WINDOW_SIZE, undefined, signal),
      staleTime: Infinity,
      gcTime: 0,
    })),
  });
  const windows = [initial, ...queries.flatMap((q) => q.data ? [q.data] : [])];
  const rows = new Map(windows.flatMap((w) => w.segments.map((s) => [s.position, s] as const)));
  const byIndex = new Map([...rows.values()].map((s) => [s.index, s]));
  if (pinned && !byIndex.has(pinned.index)) byIndex.set(pinned.index, pinned);
  const watched = [...new Set([...positions.map((p) => rows.get(p)?.index), pinned?.index])]
    .filter((i): i is number => i !== undefined).sort((a, b) => a - b);
  const current = useRef(byIndex);
  current.current = byIndex;
  const indices = useRef(watched);
  indices.current = watched;
  const refresh = useCallback(async (signal?: AbortSignal) => {
    // Explicit saves may refresh before React has committed the previous patch.
    // Read versions from the cache, not only the last rendered row references.
    const cached = new Map(qc.getQueriesData<ChapterWindow>({
      queryKey: windowKey(pid, ci, sourceOnly),
    }).flatMap(([, window]) => window?.segments.map((s) => [s.index, s] as const) || []));
    const known = indices.current.flatMap((i) => {
      const row = cached.get(i) || current.current.get(i);
      return row ? [{ index: i, revision: row.revision }] : [];
    });
    if (!known.length || sourceOnly) return [];
    const changed = [];
    for (let offset = 0; offset < known.length; offset += 256) {
      changed.push(...await api.chapterUpdates(pid, ci, known.slice(offset, offset + 256), signal));
    }
    if (signal?.aborted) return [];
    const patches = new Map(changed.map((s) => [s.index, s]));
    qc.setQueriesData<ChapterWindow>({ queryKey: windowKey(pid, ci, sourceOnly) }, (window) => {
      if (!window) return window;
      let modified = false;
      let savedDelta = 0;
      const segments = window.segments.map((segment) => {
        const patch = patches.get(segment.index);
        // An older in-flight refresh must not overwrite a newer window or edit.
        if (!patch || patch.previous_revision !== segment.revision) return segment;
        modified = true;
        savedDelta += Number(patch.target !== null) - Number(segment.target !== null);
        return { ...segment, target: patch.target, target_before_polish: patch.target_before_polish,
          revision: patch.revision };
      });
      return modified ? { ...window, segments, saved_count: window.saved_count + savedDelta } : window;
    });
    return changed;
  }, [pid, ci, sourceOnly, qc]);
  const updates = useQuery({
    queryKey: ["chapterUpdates", pid, ci, sourceOnly],
    queryFn: ({ signal }) => refresh(signal),
    enabled: !sourceOnly && watched.length > 0,
    staleTime: Infinity,
    refetchInterval: busy ? 3000 : false,
    gcTime: 0,
  });
  const refreshAfterEdit = useCallback(async () => {
    // A poll started before the edit cannot race the authoritative post-save read.
    await qc.cancelQueries({ queryKey: ["chapterUpdates", pid, ci, sourceOnly] });
    return refresh();
  }, [qc, pid, ci, sourceOnly, refresh]);
  return {
    rows,
    byIndex,
    refresh: refreshAfterEdit,
    error: queries.find((q) => q.error)?.error || updates.error,
  };
}
