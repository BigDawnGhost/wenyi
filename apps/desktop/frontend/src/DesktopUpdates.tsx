import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useDesktopI18n } from "./i18n";
import { useCredentialSafety } from "./updateSafety";

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
  async function run(action: "check" | "download" | "install" | "open_release") {
    if (action === "install" && unsafe) return;
    setPending(true);
    setError(undefined);
    if (action === "install") { setInstalling(true); setConfirm(false); }
    try {
      await invoke(`desktop_update_${action}`);
      await status.refetch();
    } catch (failure) {
      const code = String(failure);
      setError(code.includes("busy") ? "busy" : code.includes("unavailable") ? "unavailable"
        : action === "install" ? "install_failed" : action === "download" ? "download_failed" : "check_failed");
      setInstalling(false);
    } finally { setPending(false); }
  }
  const failure = error || data?.error || (status.error ? "unavailable" : undefined);
  return (
    <Card data-testid="desktop-updates">
      <CardContent className="p-5 space-y-4">
        <h2 className="font-medium">{t("updates.title")}</h2>
        <p>{t("updates.currentVersion", { version: data?.currentVersion || "—" })}</p>
        {data && <p role="status">{t(`updates.${data.phase}`)}</p>}
        {data?.mode !== "automatic" && <p>{t("updates.manual")}</p>}
        {data?.version && <p>{t("updates.version", { version: data.version })}</p>}
        {data?.notes && <pre className="whitespace-pre-wrap break-words text-sm font-sans">{data.notes}</pre>}
        {data?.phase === "downloading" && (
          <div>
            <progress aria-label={t("updates.downloading")} value={data.total ? data.downloaded : undefined} max={data.total || undefined} />
            <p>{data.downloaded} / {data.total || "—"} {t("updates.bytes")}</p>
          </div>
        )}
        {failure && <p role="alert">{t(`updates.${failure}`)}</p>}
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
          <Button variant="outline" disabled={locked} onClick={() => void run("open_release")}>{t("updates.releases")}</Button>
        </div>
        {unsafe && <p>{t("updates.unsaved")}</p>}
        {confirm && (
          <div role="alertdialog" aria-label={t("updates.install")} className="space-y-3">
            <p>{t("updates.confirm")}</p>
            <Button disabled={active || unsafe} onClick={() => void run("install")}>{t("updates.confirmInstall")}</Button>
            <Button variant="outline" disabled={locked} onClick={() => setConfirm(false)}>{t("updates.cancel")}</Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
