import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Dialog } from "@wenyi/ui/primitives";
import { SettingsSection } from "@wenyi/ui/settings";
import { useDesktopI18n } from "./i18n";
import { diagnosticCommand, type DiagnosticStatus } from "./diagnostics";

export function DiagnosticExportButton() {
  const { t } = useDesktopI18n();
  const [open, setOpen] = useState(false);
  const [range, setRange] = useState("day");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  async function save() {
    setBusy(true);
    setError(false);
    setSaved(null);
    try {
      const result = await diagnosticCommand<{ path: string } | null>("export", { range });
      if (result) setSaved(result.path);
    } catch { setError(true); }
    finally { setBusy(false); }
  }
  return <>
    <Button variant="outline" onClick={() => setOpen(true)}>{t("diagnostics.export")}</Button>
    <Dialog open={open} onClose={() => setOpen(false)} closeDisabled={busy}
      title={t("diagnostics.export")} description={t("diagnostics.privacy")}>
      <div className="space-y-4">
        <label className="block text-sm">
          {t("diagnostics.range")}
          <select className="ml-3 rounded border bg-background p-2" value={range}
            disabled={busy} onChange={event => setRange(event.target.value)}>
            <option value="day">{t("diagnostics.day")}</option>
            <option value="session">{t("diagnostics.session")}</option>
            <option value="week">{t("diagnostics.week")}</option>
          </select>
        </label>
        <p className="text-sm">{t("diagnostics.contents")}</p>
        <p className="text-sm text-muted-foreground">{t("diagnostics.partial")}</p>
        {busy && <p role="status">{t("diagnostics.saving")}</p>}
        {error && <p role="alert" className="text-destructive">{t("diagnostics.failed")}</p>}
        {saved && <p role="status" className="break-all">{t("diagnostics.saved", { path: saved })}</p>}
        <div className="flex flex-wrap gap-3">
          <Button disabled={busy} onClick={() => void save()}>{t("diagnostics.choose")}</Button>
          {busy && <Button variant="outline" onClick={() => {
            void diagnosticCommand("cancel").catch(() => setError(true));
          }}>{t("diagnostics.cancel")}</Button>}
        </div>
      </div>
    </Dialog>
  </>;
}

export function DesktopDiagnostics() {
  const { t } = useDesktopI18n();
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const [copied, setCopied] = useState(false);
  const status = useQuery({
    queryKey: ["desktopDiagnostics"],
    queryFn: () => diagnosticCommand<DiagnosticStatus>("status"),
    refetchInterval: query => (query.state.data?.detailSeconds ?? 0) > 0 ? 1000 : false,
    retry: false,
  });
  const data = status.data;
  async function action(name: string, args?: Record<string, unknown>) {
    setPending(true);
    setFailed(false);
    try {
      await diagnosticCommand(name, args);
      await status.refetch();
    } catch { setFailed(true); }
    finally { setPending(false); }
  }
  async function copy() {
    setFailed(false);
    try {
      // Intentionally exclude directory, browser URL and platform/user-agent strings.
      const snapshot = await diagnosticCommand<DiagnosticStatus>("status");
      await navigator.clipboard.writeText(
        `Wenyi Desktop ${snapshot.version}\nError ID: ${snapshot.lastErrorId ?? "none"}\n` +
        `Log storage: ${snapshot.degraded ? "degraded" : "available"}\n` +
        `Dropped records: ${snapshot.dropped}\nDetailed logging: ${snapshot.detailSeconds > 0}`,
      );
      setCopied(true);
    } catch { setFailed(true); }
  }
  return <SettingsSection title={t("diagnostics.title")} data-testid="desktop-diagnostics">
    <p className="text-sm text-muted-foreground">{t("diagnostics.retention")}</p>
    {data && <div className="space-y-2 text-sm">
      <p className="break-all">{t("diagnostics.location", { path: data.directory ?? "—" })}</p>
      <p>{t("diagnostics.size", { size: (data.bytes / 1048576).toFixed(1), dropped: data.dropped })}</p>
      {data.lastErrorId && <p>{t("diagnostics.errorId", { id: data.lastErrorId })}</p>}
      {data.degraded && <p role="alert">{t("diagnostics.degraded")}</p>}
      {data.detailSeconds > 0 && <p role="status">{t("diagnostics.remaining", { seconds: data.detailSeconds })}</p>}
    </div>}
    <div className="flex flex-wrap gap-3">
      <DiagnosticExportButton />
      <Button variant="outline" onClick={() => void copy()}>{t("diagnostics.copy")}</Button>
      <Button variant="outline" disabled={pending || !data} onClick={() => void action("detail", {
        enabled: !(data?.detailSeconds && data.detailSeconds > 0),
      })}>{t(data?.detailSeconds ? "diagnostics.stopDetail" : "diagnostics.detail")}</Button>
      <Button variant="outline" disabled={pending || !data?.directory} onClick={() => void action("open")}>{t("diagnostics.open")}</Button>
      <Button variant="outline" disabled={pending} onClick={() => void action("clear")}>{t("diagnostics.clear")}</Button>
      <Button variant="outline" disabled={pending} onClick={() => void status.refetch()}>{t("diagnostics.refresh")}</Button>
    </div>
    {copied && <p role="status">{t("diagnostics.copied")}</p>}
    {(failed || status.error) && <p role="alert" className="text-destructive">{t("diagnostics.failed")}</p>}
  </SettingsSection>;
}
