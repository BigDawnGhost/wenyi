import { useSyncExternalStore } from "react";

// Only booleans are shared; credential values never leave their owning fields.
const fields = new Set<symbol>();
const listeners = new Set<() => void>();
export function updateCredentialSafety(id: symbol, blocked: boolean) {
  if (blocked) fields.add(id);
  else fields.delete(id);
  listeners.forEach((listener) => listener());
}
export function useCredentialSafety() {
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => fields.size > 0,
  );
}
