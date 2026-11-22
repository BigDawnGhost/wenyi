import type { QueryClient } from "@tanstack/react-query";
import type { desktopComponents } from "@wenyi/shared-schema";
import { request } from "./transport";
import { guardCredentialWrite } from "./updateSafety";

// Generated from the Desktop route that answers it, so a field added in Python
// cannot silently drift away from what this module reads.
export type CredentialStatus = desktopComponents["schemas"]["CredentialStatus"];

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

export async function saveCredential(
  queryClient: QueryClient,
  connection: string,
  key: string,
) {
  const secret = key.trim();
  const body = { mode: "manual", ...(secret ? { secret } : { clear: true }) };
  const result = await guardCredentialWrite(() => request<CredentialStatus>(
    `/desktop/credentials/${encodeURIComponent(connection)}`,
    { method: "PUT", body: JSON.stringify(body) },
  ));
  await queryClient.cancelQueries({ queryKey: credentialQuery.queryKey });
  queryClient.setQueryData<Record<string, CredentialStatus>>(
    credentialQuery.queryKey, current => ({ ...current, [connection]: result }),
  );
}
