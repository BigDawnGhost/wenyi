import { request } from "./transport";

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

export function saveCredential(
  connection: string,
  body: {
    mode: "environment" | "manual";
    storage?: "auto" | "system" | "session";
    secret?: string;
    clear?: boolean;
  },
) {
  return request<CredentialStatus>(
    `/desktop/credentials/${encodeURIComponent(connection)}`,
    { method: "PUT", body: JSON.stringify(body) },
  );
}
