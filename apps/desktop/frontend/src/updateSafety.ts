import { useSyncExternalStore } from "react";

// Only booleans are shared; credential values never leave their owning fields.
const fields = new Set<symbol>();
const listeners = new Set<() => void>();
export function updateCredentialSafety(id: symbol, blocked: boolean) {
  if (blocked) fields.add(id);
  else fields.delete(id);
  listeners.forEach((listener) => listener());
}

/** Pending credential requests outlive field unmounts; block installation until settled. */
export async function guardCredentialWrite<T>(write: () => Promise<T>): Promise<T> {
  const id = Symbol("credential-write");
  updateCredentialSafety(id, true);
  try {
    return await write();
  } finally {
    updateCredentialSafety(id, false);
  }
}

export function useCredentialSafety() {
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => fields.size > 0,
  );
}
