import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button } from "@wenyi/ui/primitives";
import { SettingsSection } from "@wenyi/ui/settings";
import { request } from "./transport";
import { TransferProgress } from "./TransferProgress";
import { translateDesktop, useDesktopI18n } from "./i18n";

type Status = {
  enabled: boolean;
  installed: boolean;
  supported: boolean;
  phase: "disabled" | "checking" | "runtime" | "models" | "unpacking" | "starting" | "ready" | "stopping" | "error";
  version: string | null;
  downloaded: number;
  total: number | null;
  error: "state_failed" | "interrupted" | "install_failed" | "start_failed" | null;
  latest_version: string | null;
  update_available: boolean;
  checking_update: boolean;
  updating: boolean;
  update_error: "check_failed" | "update_busy" | "update_failed" | "rollback_failed" | null;
};
const key = ["desktopBabeldocPlugin"];
const endpoint = "/desktop/plugins/babeldoc";
type ActionFailure = Parameters<typeof translateDesktop>[1];
// The plugin router refuses actions with distinct sentences but no stable code
// yet, and every refusal shares HTTP 409. Reading a code first leaves room for
// the router to adopt one; until then the sentence is the only distinguisher,
// so an unrecognized refusal keeps the generic action message.
const actionFailures: [string, ActionFailure][] = [
  ["plugin_update_check_unavailable", "babeldoc.check_failed"],
  ["plugin_update_unavailable", "babeldoc.update_busy"],
  ["plugin_unsupported", "babeldoc.unsupported"],
  ["Plugin update check is unavailable", "babeldoc.check_failed"],
  ["Plugin update is unavailable", "babeldoc.update_busy"],
  ["BabelDOC is not available for this platform", "babeldoc.unsupported"],
];
/** The message a refused plugin action should show, from its refusal. */
function actionFailure(error: unknown): ActionFailure {
  if (typeof error !== "object" || error === null) return "babeldoc.action_failed";
  const candidate = error as { code?: unknown; detail?: unknown; message?: unknown };
  for (const value of [candidate.code, candidate.detail, candidate.message]) {
    const match = actionFailures.find(([text]) => value === text);
    if (match) return match[1];
  }
  return "babeldoc.action_failed";
}

export function BabeldocPlugin({ disabled }: { disabled: boolean }) {
  const { t } = useDesktopI18n();
  const client = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const query = useQuery({
    queryKey: key,
    queryFn: () => request<Status>(endpoint),
    // Setup and updates progress on their own; a settled plugin changes only
    // when the user acts, and each action writes the response back locally.
    refetchInterval: (query) =>
      query.state.data?.updating || query.state.data?.checking_update
        ? 1000
        : ["disabled", "ready", "error"].includes(query.state.data?.phase ?? "")
          ? 30_000
          : 1000,
    retry: false,
  });
  const action = useMutation({
    mutationFn: (operation: "enable" | "disable" | "check-update" | "update" | "cancel-update") =>
      request<Status>(`${endpoint}/${operation}`, { method: "POST" }),
    onSuccess: (value) => { client.setQueryData(key, value); setConfirm(false); },
  });
  const data = query.data;
  const active = data && (data.updating || !["disabled", "ready", "error"].includes(data.phase));
  const downloading = data?.phase === "runtime" || data?.phase === "models";
  return (
    <SettingsSection title={t("babeldoc.title")} data-testid="babeldoc-plugin">
      <div className="flex min-h-8 flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {data?.version && <span>{t("babeldoc.version", { version: data.version })}</span>}
          <Badge variant="outline" className="text-muted-foreground">AGPL-3.0-only</Badge>
          {data && !active && (
            <span role="status" className="text-muted-foreground">{t(`babeldoc.${data.phase}`)}</span>
          )}
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          {data && !active && !data.checking_update && data.phase !== "ready" && (
            <Button
              size="sm"
              disabled={disabled || action.isPending || !data.supported}
              onClick={() => action.mutate("enable")}
            >
              {t(data.phase === "error" ? "babeldoc.retry" : data.installed ? "babeldoc.enable" : "babeldoc.download")}
            </Button>
          )}
          {data?.installed && !active && (
            <Button
              size="sm"
              variant="outline"
              disabled={disabled || action.isPending || data.checking_update || !data.supported}
              onClick={() => action.mutate("check-update")}
            >{t(data.checking_update ? "babeldoc.checkingUpdate" : "babeldoc.checkUpdate")}</Button>
          )}
          {data?.update_available && !active && !data.checking_update && (
            <Button
              size="sm"
              disabled={disabled || action.isPending}
              onClick={() => setConfirm(true)}
            >{t("babeldoc.update")}</Button>
          )}
          {data?.updating && (
            <Button
              size="sm"
              variant="outline"
              disabled={disabled || action.isPending}
              onClick={() => action.mutate("cancel-update")}
            >{t("babeldoc.cancelUpdate")}</Button>
          )}
          {data?.enabled && !data.updating && (
            <Button
              size="sm"
              variant="outline"
              disabled={disabled || action.isPending || data.phase === "stopping"}
              onClick={() => action.mutate("disable")}
            >{t(downloading ? "babeldoc.cancelDownload" : active ? "babeldoc.cancel" : "babeldoc.disable")}</Button>
          )}
          {query.error && (
            <Button size="sm" variant="outline" onClick={() => void query.refetch()}>{t("babeldoc.retry")}</Button>
          )}
        </div>
      </div>
      {data && active && (
        <TransferProgress
          label={t(`babeldoc.${data.phase}`)}
          downloaded={downloading ? data.downloaded : undefined}
          total={downloading ? data.total : undefined}
        />
      )}
      {data?.latest_version && !data.checking_update && (
        <p role="status" className="text-sm text-muted-foreground">
          {t(data.update_available ? "babeldoc.availableVersion" : "babeldoc.current", {
            version: data.latest_version,
          })}
        </p>
      )}
      {confirm && data?.update_available && !data.updating && (
        <div role="alertdialog" aria-label={t("babeldoc.update")} className="space-y-3">
          <p className="text-sm">{t("babeldoc.confirmUpdate", { version: data.latest_version })}</p>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={disabled || action.isPending}
              onClick={() => action.mutate("update")}
            >{t("babeldoc.confirmUpdateAction")}</Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setConfirm(false)}
            >{t("updates.cancel")}</Button>
          </div>
        </div>
      )}
      {data?.supported === false && <p className="text-sm">{t("babeldoc.unsupported")}</p>}
      {(query.error || action.error || data?.error || data?.update_error) && (
        <p role="alert" className="text-sm text-destructive">
          {action.error
            ? t(actionFailure(action.error))
            : data?.update_error ? t(`babeldoc.${data.update_error}`)
              : data?.error ? t(`babeldoc.${data.error}`) : t("babeldoc.unavailable")}
        </p>
      )}
    </SettingsSection>
  );
}
