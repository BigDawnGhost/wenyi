import { useEffect, useState } from "react";
import { updateCredentialSafety } from "./updateSafety";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { credentialQuery, saveCredential, type CredentialStatus } from "./credentials";
import { useDesktopI18n } from "./i18n";
import type { CredentialFieldProps } from "@wenyi/ui/platform";
import { ErrorNotice } from "@/components/ui/data";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/form";

export function DesktopCredential({
  connection, saved, children,
}: CredentialFieldProps) {
  const { t } = useDesktopI18n();
  const credentials = useQuery(credentialQuery);
  const status = credentials.data?.[connection];
  const refresh = () => void credentials.refetch();
  const queryClient = useQueryClient();
  const [secret, setSecret] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(false);
  const [safetyId] = useState(() => Symbol());
  useEffect(() => {
    updateCredentialSafety(safetyId, secret.length > 0 || pending);
    return () => updateCredentialSafety(safetyId, false);
  }, [safetyId, secret, pending]);
  const save = async (action: "save" | "clear" | "environment") => {
    if (action === "save" && !secret.trim()) return;
    setPending(true);
    setError(false);
    try {
      // Only the secret-free response enters the query cache.
      const result = await saveCredential(connection, {
        mode: action === "environment" ? "environment" : "manual",
        ...(action === "save" ? { secret } : { clear: true }),
      });
      queryClient.setQueryData<Record<string, CredentialStatus>>(
        ["credentials"], (current) => ({ ...current, [connection]: result }),
      );
      setSecret("");
    } catch {
      setError(true);
    } finally {
      setPending(false);
    }
  };
  return (
    <fieldset className="space-y-3 border-t pt-3" disabled={pending}>
      <legend className="text-sm font-medium">{t("credentials.title")}</legend>
      <ErrorNotice error={credentials.error} />
      {!saved && <p className="text-sm">{t("credentials.saveConnection")}</p>}
      <Label htmlFor={`credential-secret-${connection}`}>{t("credentials.key")}</Label>
      <Input
        id={`credential-secret-${connection}`}
        type="password"
        disabled={!saved || !status}
        autoComplete="new-password"
        spellCheck={false}
        value={secret}
        placeholder={t("credentials.noEcho")}
        onChange={(event) => setSecret(event.target.value)}
      />
      <p className="text-sm" role="status">
        {t(
          status && !status.requires_key && !status.environment && status.mode === "environment"
            ? "credentials.notRequired"
            : status?.available ? "credentials.available" : "credentials.missing",
        )}
        {status?.available && ` · ${t(status.mode === "environment"
          ? "credentials.environment" : status.storage === "session"
            ? "credentials.session" : "credentials.system")}`}
      </p>
      {status?.storage === "session" && (
        <p className="text-sm">{t("credentials.sessionHelp")}</p>
      )}
      <p className="text-xs text-muted-foreground">{t("credentials.autoHelp")}</p>
      {error && <p role="alert" className="text-sm text-destructive">{t("credentials.failed")}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={!saved || !status || !secret.trim()} onClick={() => void save("save")}>
          {t("credentials.save")}
        </Button>
        {status?.mode === "manual" && (
          <Button type="button" variant="outline" disabled={!saved} onClick={() => void save("clear")}>
            {t("credentials.clear")}
          </Button>
        )}
      </div>
      <details>
        <summary className="cursor-pointer text-sm">{t("credentials.advanced")}</summary>
        <div className="space-y-3 pt-3">
          {children}
          <p className="text-sm">{t("credentials.environmentHelp", {
            name: status?.environment || "—",
          })}</p>
          <Button type="button" variant="outline" disabled={!saved || !status} onClick={() => void save("environment")}>
            {t("credentials.useEnvironment")}
          </Button>
          <p className="text-xs text-muted-foreground">{t("credentials.offline")}</p>
          <Button type="button" variant="outline" onClick={refresh}>{t("credentials.refresh")}</Button>
        </div>
      </details>
    </fieldset>
  );
}
