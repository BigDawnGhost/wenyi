import { useSyncExternalStore } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { request } from "./transport";
import { guardCredentialWrite, updateCredentialSafety } from "./updateSafety";

export type CredentialStatus = {
  mode: "environment" | "manual";
  storage: "system" | "session" | null;
  available: boolean;
  environment: string | null;
  system_storage_available: boolean;
  requires_key: boolean;
};

export const credentialQuery = {
  queryKey: ["credentials"],
  queryFn: () => request<Record<string, CredentialStatus>>("/desktop/credentials"),
  retry: false,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
} as const;

/** The saved key, shown masked in its field. Never cached by React Query. */
export function readCredential(connection: string) {
  return request<{ secret: string | null }>(
    `/desktop/credentials/${encodeURIComponent(connection)}/secret`,
  ).then((result) => result.secret ?? "");
}

export function saveCredential(
  connection: string,
  body: {
    mode: "environment" | "manual";
    storage?: "auto" | "system" | "session";
    secret?: string;
    clear?: boolean;
  },
) {
  return guardCredentialWrite(() => request<CredentialStatus>(
    `/desktop/credentials/${encodeURIComponent(connection)}`,
    { method: "PUT", body: JSON.stringify(body) },
  ));
}

/**
 * Field values per connection, held only in memory. `saved` is what the
 * credential store currently returns, so an unchanged field writes nothing;
 * a cleared field removes the stored key on the next configuration save.
 */
type Entry = { value: string; saved: string };
const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
const safetyId = Symbol("credential-drafts");
let version = 0;
const dirty = (entry: Entry) => entry.value !== entry.saved;
function changed() {
  version++;
  updateCredentialSafety(safetyId, [...entries.values()].some(dirty));
  listeners.forEach((listener) => listener());
}
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

export const credentialDrafts = {
  get: (connection: string) => entries.get(connection),
  /** Record the stored key once loaded; a value the user already typed is kept. */
  load(connection: string, saved: string) {
    const entry = entries.get(connection);
    if (entry && dirty(entry)) entries.set(connection, { ...entry, saved });
    else entries.set(connection, { value: saved, saved });
    changed();
  },
  set(connection: string, value: string) {
    const entry = entries.get(connection);
    entries.set(connection, { value, saved: entry?.saved ?? "" });
    changed();
  },
  /** A store write made outside the draft (clear, switch to environment). */
  stored(connection: string, saved: string) {
    entries.set(connection, { value: saved, saved });
    changed();
  },
  rename(from: string, to: string) {
    const entry = entries.get(from);
    if (!entry) return;
    entries.delete(from);
    entries.set(to, entry);
    changed();
  },
  /** Forget connections that no longer exist in the draft, e.g. deleted before saving. */
  retain(connections: Iterable<string>) {
    const keep = new Set(connections);
    let removed = false;
    for (const connection of [...entries.keys()])
      if (!keep.has(connection)) removed = entries.delete(connection) || removed;
    if (removed) changed();
  },
  /** Write every changed field; failed ones stay changed so the next save retries them. */
  async commit(queryClient: QueryClient, connections: Iterable<string>) {
    credentialDrafts.retain(connections);
    let failed = false;
    for (const [connection, entry] of [...entries]) {
      if (!dirty(entry)) continue;
      const value = entry.value.trim();
      try {
        const result = await saveCredential(
          connection,
          value ? { mode: "manual", secret: value } : { mode: "manual", clear: true },
        );
        queryClient.setQueryData<Record<string, CredentialStatus>>(
          credentialQuery.queryKey, (current) => ({ ...current, [connection]: result }),
        );
        const current = entries.get(connection);
        // Keep a newer value typed while the request was in flight.
        if (current) entries.set(connection, { value: current.value, saved: value });
      } catch {
        failed = true;
      }
    }
    changed();
    if (failed) throw new Error("credential-save-failed");
  },
};

export function useCredentialEntry(connection: string) {
  useSyncExternalStore(subscribe, () => version);
  return credentialDrafts.get(connection);
}

export function useCredentialDraftsPending() {
  return useSyncExternalStore(subscribe, () => [...entries.values()].some(dirty));
}

/** The key to use for a draft model listing: what is in the field right now. */
export function credentialForListing(connection: string) {
  return entries.get(connection)?.value.trim() || undefined;
}
