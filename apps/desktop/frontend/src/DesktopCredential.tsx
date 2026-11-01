import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  credentialDrafts,
  credentialQuery,
  readCredential,
  saveCredential,
  useCredentialEntry,
  type CredentialStatus,
} from "./credentials";
import { useDesktopI18n } from "./i18n";
import type { CredentialFieldProps } from "@wenyi/ui/platform";
import { ErrorNotice, Button, Label, PasswordInput } from "@wenyi/ui/primitives";

/**
 * API key field. A saved manual key is loaded into the field masked with dots;
 * the eye button reveals it. Edits are written with the next configuration save.
 */
export function DesktopCredential({ connection, children }: CredentialFieldProps) {
  const { t } = useDesktopI18n();
  const credentials = useQuery(credentialQuery);
  const status = credentials.data?.[connection];
  const queryClient = useQueryClient();
  const entry = useCredentialEntry(connection);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(false);
  const manual = status?.mode === "manual" && status.available;
  // Load the stored key once per connection so the field shows it masked.
  useEffect(() => {
    if (!manual || credentialDrafts.get(connection)) return;
    let active = true;
    readCredential(connection)
      .then((secret) => { if (active) credentialDrafts.load(connection, secret); })
      // An unreadable saved key must not block entering a replacement.
      .catch(() => { if (active) credentialDrafts.load(connection, ""); });
    return () => { active = false; };
  }, [connection, manual]);
  const write = async (mode: "manual" | "environment") => {
    setPending(true);
    setError(false);
    try {
      const result = await saveCredential(connection, { mode, clear: true });
      queryClient.setQueryData<Record<string, CredentialStatus>>(
        credentialQuery.queryKey, (current) => ({ ...current, [connection]: result }),
      );
      credentialDrafts.stored(connection, "");
    } catch {
      setError(true);
    } finally {
      setPending(false);
    }
  };
  const environment = status?.mode === "environment";
  const placeholder = environment && status.available
    ? t("credentials.environmentPlaceholder", { name: status.environment || "" })
    : status && !status.requires_key && environment
      ? t("credentials.notRequired")
      : t("credentials.placeholder");
  const inputId = `credential-secret-${connection}`;
  return (
    <div className="space-y-2">
      <ErrorNotice error={credentials.error} />
      <Label htmlFor={inputId}>{t("credentials.key")}</Label>
      <PasswordInput
        id={inputId}
        value={entry?.value ?? ""}
        placeholder={placeholder}
        showLabel={t("credentials.show")}
        hideLabel={t("credentials.hide")}
        onChange={(event) => credentialDrafts.set(connection, event.target.value)}
      />
      {manual && status.storage === "session" && (
        <p className="text-xs text-muted-foreground">{t("credentials.sessionHelp")}</p>
      )}
      {error && <p role="alert" className="text-sm text-destructive">{t("credentials.failed")}</p>}
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">{t("credentials.advanced")}</summary>
        <fieldset className="space-y-3 pt-3" disabled={pending}>
          {children}
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!status || environment}
              onClick={() => void write("environment")}
            >
              {t("credentials.useEnvironment")}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => void credentials.refetch()}>
              {t("credentials.refresh")}
            </Button>
          </div>
        </fieldset>
      </details>
    </div>
  );
}
