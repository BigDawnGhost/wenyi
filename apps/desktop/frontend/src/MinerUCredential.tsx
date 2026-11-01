import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Label, PasswordInput } from "@wenyi/ui/primitives";
import { useDesktopI18n } from "./i18n";
import {
  clearMineruCredential,
  mineruCredentialQuery,
  readMineruCredential,
  saveMineruCredential,
} from "./mineruCredentials";
import { updateCredentialSafety } from "./updateSafety";

// Wait for a pause in typing before writing, so each keystroke is not a vault write.
const AUTOSAVE_DELAY_MS = 800;

/**
 * MinerU key field using the same input as a connection's API key. A manually saved
 * key is loaded masked; edits save automatically after a pause or on blur, and
 * clearing the field removes the key.
 */
export function MinerUCredential() {
  const { t } = useDesktopI18n();
  const query = useQuery(mineruCredentialQuery);
  const queryClient = useQueryClient();
  const status = query.data;
  const environmentSelected = status?.mode === "environment";
  const manualSaved = status?.mode === "manual" && status.available;
  const [value, setValue] = useState("");
  // The value last loaded from or written to the store; the field is dirty when it differs.
  const [stored, setStored] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const [saved, setSaved] = useState(false);
  const [safetyId] = useState(() => Symbol("mineru"));
  // A failed value is not retried until the user edits it again.
  const attempted = useRef<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const dirty = stored !== null && value.trim() !== stored;
  useEffect(() => {
    updateCredentialSafety(safetyId, dirty || pending);
    return () => updateCredentialSafety(safetyId, false);
  }, [safetyId, dirty, pending]);
  useEffect(() => () => clearTimeout(timer.current), []);
  // Load the saved key once so it shows masked; environment keys are never read back.
  useEffect(() => {
    if (!status || stored !== null) return;
    if (environmentSelected || !manualSaved) {
      setStored("");
      return;
    }
    let active = true;
    readMineruCredential()
      .then((secret) => {
        if (!active) return;
        setStored(secret);
        setValue((current) => current || secret);
      })
      // An unreadable saved key must not block entering a replacement.
      .catch(() => { if (active) setStored(""); });
    return () => { active = false; };
  }, [status, stored, environmentSelected, manualSaved]);
  useEffect(() => {
    if (environmentSelected) setValue("");
  }, [environmentSelected]);
  const save = useCallback(async (next: string) => {
    clearTimeout(timer.current);
    const secret = next.trim();
    if (pending || environmentSelected || !status || stored === null) return;
    if (secret === stored || secret === attempted.current) return;
    attempted.current = secret;
    // Register synchronously as well: imperative writes must block installation.
    updateCredentialSafety(safetyId, true);
    setPending(true);
    setFailed(false);
    setSaved(false);
    try {
      const result = secret
        ? await saveMineruCredential({ secret })
        : await clearMineruCredential();
      // Cache only the secret-free status, never mutation variables or secrets.
      queryClient.setQueryData(mineruCredentialQuery.queryKey, result);
      setStored(secret);
      setSaved(true);
    } catch {
      setFailed(true);
    } finally {
      setPending(false);
    }
  }, [pending, environmentSelected, status, stored, safetyId, queryClient]);
  // Save once typing pauses; a newer edit restarts the delay.
  useEffect(() => {
    if (!dirty || pending) return;
    timer.current = setTimeout(() => void save(value), AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer.current);
  }, [value, dirty, pending, save]);
  return (
    <fieldset className="min-w-0 space-y-2">
      <legend className="sr-only">{t("mineru.credential")}</legend>
      {(query.isError || failed) && (
        <p role="alert" className="text-sm text-destructive">{t("mineru.failed")}</p>
      )}
      {environmentSelected && !status.available && (
        <p role="alert" className="text-sm text-destructive">{t("mineru.environmentInvalid")}</p>
      )}
      <Label htmlFor="mineru-secret">{t("mineru.key")}</Label>
      <PasswordInput
        id="mineru-secret"
        value={value}
        disabled={!status || environmentSelected}
        placeholder={t(environmentSelected ? "mineru.environmentPlaceholder" : "mineru.placeholder")}
        showLabel={t("mineru.show")}
        hideLabel={t("mineru.hide")}
        onChange={(event) => {
          setValue(event.target.value);
          attempted.current = null;
          setSaved(false);
          setFailed(false);
        }}
        onBlur={() => void save(value)}
      />
      {saved && !dirty && (
        <p role="status" className="text-xs text-muted-foreground">
          {t(stored ? "mineru.saved" : "mineru.cleared")}
        </p>
      )}
      {!environmentSelected && status?.available && status.storage === "session" && (
        <p className="text-xs text-muted-foreground">{t("mineru.sessionWarning")}</p>
      )}
    </fieldset>
  );
}
