import type { ActivityServices } from "../platform/contracts";

/** Warm one page at a time after the first paint, only while the host is visible. */
export function warmPages(
  loaders: ReadonlyArray<() => Promise<unknown>>,
  activity: ActivityServices,
) {
  let index = 0;
  let stopped = false;
  let loading = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let idle: number | undefined;

  const cancel = () => {
    clearTimeout(timer);
    timer = undefined;
    if (idle !== undefined) window.cancelIdleCallback(idle);
    idle = undefined;
  };
  const schedule = () => {
    if (stopped || loading || !activity.isForeground() || index >= loaders.length) return;
    cancel();
    timer = setTimeout(() => {
      timer = undefined;
      if (window.requestIdleCallback) idle = window.requestIdleCallback(load);
      else load();
    }, 1000);
  };
  const load = () => {
    idle = undefined;
    if (stopped || !activity.isForeground()) return;
    loading = true;
    // A failed warm-up is not a page failure. Navigation owns its error boundary.
    void Promise.resolve().then(loaders[index++]).catch(() => {}).finally(() => {
      loading = false;
      schedule();
    });
  };
  const unsubscribe = activity.subscribe(() => {
    cancel();
    schedule();
  });
  schedule();
  return () => {
    stopped = true;
    cancel();
    unsubscribe();
  };
}
