import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SettingsSection } from "@/features/settings/SettingsSection";
import { request } from "./transport";
import { TransferProgress } from "./TransferProgress";
import { useDesktopI18n } from "./i18n";

type Status = {
  enabled: boolean;
  installed: boolean;
  supported: boolean;
  phase: "disabled" | "checking" | "runtime" | "models" | "unpacking" | "starting" | "ready" | "stopping" | "error";
  version: string | null;
  downloaded: number;
  total: number | null;
  error: "state_failed" | "interrupted" | "install_failed" | "start_failed" | null;
};
const key = ["desktopBabeldocPlugin"];
const endpoint = "/desktop/plugins/babeldoc";

export function BabeldocPlugin({ disabled }: { disabled: boolean }) {
  const { t } = useDesktopI18n();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: key, queryFn: () => request<Status>(endpoint), refetchInterval: 1000, retry: false,
  });
  const action = useMutation({
    mutationFn: (operation: "enable" | "disable") =>
      request<Status>(`${endpoint}/${operation}`, { method: "POST" }),
    onSuccess: (value) => { client.setQueryData(key, value); },
  });
  const data = query.data;
  const active = data && !["disabled", "ready", "error"].includes(data.phase);
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
          {data && !active && data.phase !== "ready" && (
            <Button
              size="sm"
              disabled={disabled || action.isPending || !data.supported}
              onClick={() => action.mutate("enable")}
            >
              {t(data.phase === "error" ? "babeldoc.retry" : data.installed ? "babeldoc.enable" : "babeldoc.download")}
            </Button>
          )}
          {data?.enabled && (
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
      {data?.supported === false && <p className="text-sm">{t("babeldoc.unsupported")}</p>}
      {(query.error || action.error || data?.error) && (
        <p role="alert" className="text-sm text-destructive">
          {action.error ? t("babeldoc.action_failed")
            : data?.error ? t(`babeldoc.${data.error}`) : t("babeldoc.unavailable")}
        </p>
      )}
    </SettingsSection>
  );
}
