import { request } from "./transport";
import { guardCredentialWrite } from "./updateSafety";

export type MinerUCredentialStatus = {
  mode: "environment" | "manual";
  storage: "system" | "session" | null;
  available: boolean;
  environment: string | null;
  system_storage_available: boolean;
  requires_key: boolean;
};

const endpoint = "/desktop/external-credentials/mineru";
export const mineruCredentialQuery = {
  queryKey: ["external-credentials", "mineru"],
  queryFn: () => request<MinerUCredentialStatus>(endpoint),
  retry: false,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
} as const;

export function saveMineruCredential(body: { secret: string }) {
  return guardCredentialWrite(() => request<MinerUCredentialStatus>(endpoint, {
    method: "PUT",
    body: JSON.stringify(body),
  }));
}

/** The manually saved key, shown masked in its field. Never cached by React Query. */
export function readMineruCredential() {
  return request<{ secret: string | null }>(`${endpoint}/secret`).then(
    (result) => result.secret ?? "",
  );
}

export function clearMineruCredential() {
  return guardCredentialWrite(() => request<MinerUCredentialStatus>(endpoint, {
    method: "PUT",
    body: JSON.stringify({ clear: true }),
  }));
}
