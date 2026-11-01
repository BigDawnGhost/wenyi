import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { SettingsSection } from "@/features/settings/SettingsSection";
import { useDesktopI18n } from "./i18n";
import { useCredentialSafety } from "./updateSafety";
import { ReleaseNotes } from "./ReleaseNotes";
import { TransferProgress } from "./TransferProgress";
import { ipcFailure } from "./ipcError";

type Status = {
  currentVersion: string;
  mode: "automatic" | "manual" | "unconfigured";
  phase: "idle" | "checking" | "current" | "available" | "downloading" | "ready" | "installing" | "error";
  version?: string;
  notes?: string;
  downloaded: number;
  total?: number;
  error?: "check_failed" | "download_failed" | "install_failed" | "busy" | "unavailable";
};
// Command code -> the label this page already knows how to render. The codes
// come from apps/desktop/src/{updates,native_export,native_drop,diagnostics_ui}.rs.
const SUCCESS_LABELS: Record<string, NonNullable<Status["error"]>> = {
  update_busy: "busy",
  update_unavailable: "unavailable",
  update_check_failed: "check_failed",
  update_download_failed: "download_failed",
  update_install_failed: "install_failed",
};

function invoke<T>(command: string): Promise<T> {
  const native = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke: <R>(command: string) => Promise<R> };
  }).__TAURI_INTERNALS__;
  if (!native) return Promise.reject("unavailable");
  return native.invoke<T>(command);
}

export function DesktopUpdates({ blocked, onInstalling }: {
  blocked: boolean; onInstalling: (value: boolean) => void;
}) {
  const { t } = useDesktopI18n();
  const credentialsBlocked = useCredentialSafety();
  const [confirm, setConfirm] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<Status["error"]>();
  const status = useQuery({
    queryKey: ["desktopUpdateStatus"],
    queryFn: () => invoke<Status>("desktop_update_status"),
    refetchInterval: 1000,
    retry: false,
  });
  const data = status.data;
  const locked = installing || data?.phase === "installing";
  useEffect(() => { onInstalling(locked); }, [locked, onInstalling]);
  const unsafe = blocked || credentialsBlocked;
  const active = pending || locked || data?.phase === "checking" || data?.phase === "downloading";
  async function run(action: "check" | "download" | "install") {
    if (action === "install" && unsafe) return;
    setPending(true);
    setError(undefined);
    if (action === "install") { setInstalling(true); setConfirm(false); }
    try {
      await invoke(`desktop_update_${action}`);
      await status.refetch();
    } catch (failure) {
      // The native command reports a stable code; map it to the label the
      // settings page shows. An unknown code falls back to the action's own
      // failure label, which is what the old substring search did by default.
      const { code } = ipcFailure(failure);
      setError(SUCCESS_LABELS[code] ?? (action === "install" ? "install_failed"
        : action === "download" ? "download_failed" : "check_failed"));
      setInstalling(false);
    } finally { setPending(false); }
  }
  const failure = error || data?.error || (status.error ? "unavailable" : undefined);
  return (
    <SettingsSection title={t("updates.title")} data-testid="desktop-updates" className="first-of-type:border-t first-of-type:pt-6">
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <p className="font-medium">{t("updates.currentVersion", { version: data?.currentVersion || "—" })}</p>
          {data?.version && <p className="text-muted-foreground">{t("updates.version", { version: data.version })}</p>}
        </div>
        {data && (["checking", "downloading", "installing"].includes(data.phase) ? (
          <TransferProgress
            label={t(`updates.${data.phase}`)}
            downloaded={data.phase === "downloading" ? data.downloaded : undefined}
            total={data.phase === "downloading" ? data.total : undefined}
          />
        ) : <p role="status" className="text-sm text-muted-foreground">{t(`updates.${data.phase}`)}</p>)}
        {data?.mode !== "automatic" && <p className="text-sm text-muted-foreground">{t("updates.manual")}</p>}
        {failure && <p role="alert" className="text-sm text-destructive">{t(`updates.${failure}`)}</p>}
        <div className="flex flex-wrap gap-3">
          <Button variant="outline" disabled={active} onClick={() => void run("check")}>
            {t(failure ? "updates.retry" : "updates.check")}
          </Button>
          {data?.mode === "automatic" && (data.phase === "available" || failure === "download_failed") && (
            <Button disabled={active} onClick={() => void run("download")}>{t("updates.download")}</Button>
          )}
          {data?.mode === "automatic" && (data.phase === "ready" || failure === "busy" || failure === "install_failed") && (
            <Button disabled={active || unsafe} onClick={() => setConfirm(true)}>{t("updates.install")}</Button>
          )}
          <Button variant="outline" disabled={locked || !data?.version} onClick={() => setNotesOpen(true)}>
            {t("updates.viewNotes")}
          </Button>
        </div>
        <ReleaseNotes
          open={notesOpen}
          onClose={() => setNotesOpen(false)}
          version={data?.version}
          notes={data?.notes}
        />
        {unsafe && <p className="text-sm text-muted-foreground">{t("updates.unsaved")}</p>}
        {confirm && (
          <div role="alertdialog" aria-label={t("updates.install")} className="space-y-3">
            <p>{t("updates.confirm")}</p>
            <Button disabled={active || unsafe} onClick={() => void run("install")}>{t("updates.confirmInstall")}</Button>
            <Button variant="outline" disabled={locked} onClick={() => setConfirm(false)}>{t("updates.cancel")}</Button>
          </div>
        )}
    </SettingsSection>
  );
}
