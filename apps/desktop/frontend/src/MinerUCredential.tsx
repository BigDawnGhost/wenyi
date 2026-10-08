import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/form";
import { useDesktopI18n } from "./i18n";
import { mineruCredentialQuery, saveMineruCredential } from "./mineruCredentials";
import { updateCredentialSafety } from "./updateSafety";

export function MinerUCredential() {
  const { t } = useDesktopI18n();
  const query = useQuery(mineruCredentialQuery);
  const queryClient = useQueryClient();
  const status = query.data;
  const environmentSelected = status?.mode === "environment";
  const [secret, setSecret] = useState("");
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const [saved, setSaved] = useState(false);
  const [safetyId] = useState(() => Symbol("mineru"));
  useEffect(() => {
    updateCredentialSafety(safetyId, secret.length > 0 || pending);
    return () => updateCredentialSafety(safetyId, false);
  }, [safetyId, secret, pending]);
  useEffect(() => {
    if (environmentSelected) setSecret("");
  }, [environmentSelected]);
  const save = async () => {
    if (pending || environmentSelected || !status || !secret.trim()) return;
    // Register synchronously as well: imperative writes must block installation.
    updateCredentialSafety(safetyId, true);
    setPending(true);
    setFailed(false);
    setSaved(false);
    try {
      const result = await saveMineruCredential({ secret });
      // Cache only the secret-free status, never mutation variables or secrets.
      queryClient.setQueryData(mineruCredentialQuery.queryKey, result);
      setSecret("");
      setSaved(true);
    } catch {
      setFailed(true);
    } finally {
      setPending(false);
    }
  };
  return (
    <fieldset className="space-y-3" disabled={pending}>
      <legend className="sr-only">{t("mineru.credential")}</legend>
      {(query.isError || failed) && (
        <p role="alert" className="text-sm text-destructive">{t("mineru.failed")}</p>
      )}
      {environmentSelected && !status.available && (
        <p role="alert" className="text-sm text-destructive">{t("mineru.environmentInvalid")}</p>
      )}
      <Label htmlFor="mineru-secret">{t("mineru.key")}</Label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input id="mineru-secret" type="password" autoComplete="new-password"
          className="min-w-0 flex-1"
          spellCheck={false} value={secret} disabled={!status || environmentSelected}
          placeholder={t(environmentSelected ? "mineru.environmentPlaceholder"
            : status?.available ? "mineru.savedPlaceholder" : "mineru.placeholder")}
          onChange={event => {
            setSecret(event.target.value);
            setSaved(false);
            setFailed(false);
          }} />
        <Button type="button" disabled={!status || environmentSelected || !secret.trim()}
          onClick={() => void save()}>
          {t("mineru.save")}
        </Button>
      </div>
      {saved && <p role="status" className="text-sm">{t("mineru.saved")}</p>}
      {!environmentSelected && status?.available && status.storage === "session" && (
        <p className="text-sm text-muted-foreground">{t("mineru.sessionWarning")}</p>
      )}
    </fieldset>
  );
}
