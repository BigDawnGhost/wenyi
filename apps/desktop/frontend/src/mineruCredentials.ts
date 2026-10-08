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
