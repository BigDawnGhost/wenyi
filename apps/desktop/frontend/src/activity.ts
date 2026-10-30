import { focusManager, type QueryClient } from "@tanstack/react-query";
import { credentialQuery } from "./credentials";
import { runtimeStatus } from "./runtime";

function isForeground() {
  return window.__WENYI_DESKTOP_BACKGROUND__ !== true &&
    document.visibilityState !== "hidden";
}

function subscribe(listener: () => void) {
  window.addEventListener("wenyi:desktop-visibility", listener);
  document.addEventListener("visibilitychange", listener);
  return () => {
    window.removeEventListener("wenyi:desktop-visibility", listener);
    document.removeEventListener("visibilitychange", listener);
  };
}

export const activity = { isForeground, subscribe };

function reconcileQueries(queryClient: QueryClient) {
  const predicate = (query: { queryKey: readonly unknown[] }) =>
    query.queryKey[0] !== credentialQuery.queryKey[0];
  const active = queryClient.getQueryCache().findAll({ type: "active", predicate });
  // Inactive cached pages must also become stale, without fetching them.
  void queryClient.invalidateQueries({ predicate, refetchType: "none" });
  for (const query of active) {
    const wasFetching = query.state.fetchStatus === "fetching";
    const filters = { queryKey: query.queryKey, exact: true, type: "active" as const };
    void queryClient.refetchQueries(filters, { cancelRefetch: false }).then(() => {
      // A pre-hide request may have captured an old running snapshot. Let it
      // finish, then read again; otherwise it can swallow the only reconciliation.
      // Each query waits independently, and navigation or hiding stops the tail.
      if (wasFetching && isForeground() && runtimeStatus() === "ready")
        return queryClient.refetchQueries(filters, { cancelRefetch: false });
    });
  }
}

export function configureQueryActivity(queryClient: QueryClient) {
  // Native minimization is not consistently reflected in WebView page visibility.
  // Keep ordinary blur out of this policy: a visible, unfocused window is still useful.
  focusManager.setEventListener(setFocused => {
    let foreground = isForeground();
    setFocused(foreground);
    return subscribe(() => {
      const next = isForeground();
      if (next === foreground) return;
      foreground = next;
      setFocused(next);
      if (next && runtimeStatus() === "ready") {
        // Background socket updates are deliberately ignored by the UI. Reconcile
        // durable state once on return, without cancelling already-running requests.
        // Vault reads remain explicitly user-triggered, even after a failed read.
        reconcileQueries(queryClient);
      }
    });
  });
}
