import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { ModelPingResult } from "../../api/types";
import { useI18n, type MessageKey } from "../../i18n";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { ErrorNotice } from "../../components/ui/data";

// Provider-independent failure categories from wenyi_core.llm.errors and the probe itself.
const categoryMessages: Record<string, MessageKey> = {
  ok: "connectivity.ok",
  reachable: "connectivity.reachable",
  missing_credential: "connectivity.missingCredential",
  authentication_failed: "connectivity.authenticationFailed",
  insufficient_balance: "connectivity.insufficientBalance",
  permission_denied: "connectivity.permissionDenied",
  model_or_endpoint_not_found: "connectivity.notFound",
  invalid_request: "connectivity.invalidRequest",
  rate_limited: "connectivity.rateLimited",
  timeout: "connectivity.timeout",
  connection_failed: "connectivity.connectionFailed",
  provider_unavailable: "connectivity.unavailable",
};

/** Seconds a probe has been open, so a slow provider does not read as a frozen button.
 *
 * The probe is bounded by the shared request deadline, so the count always
 * ends at a failure or at results rather than running forever.
 */
function useElapsedSeconds(active: boolean): number | undefined {
  const [elapsed, setElapsed] = useState<number>();
  useEffect(() => {
    if (!active) {
      setElapsed(undefined);
      return;
    }
    const started = performance.now();
    setElapsed(0);
    const timer = window.setInterval(
      () => setElapsed(Math.floor((performance.now() - started) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [active]);
  return elapsed;
}

/** Live test of this saved provider; the dialog owns saving unsaved edits. */
export function ModelConnectivity({
  connection,
  saved,
  disabled,
}: {
  connection: string;
  saved: boolean;
  disabled: boolean;
}) {
  const { t } = useI18n();
  const ping = useMutation({ mutationFn: () => api.pingModels(connection) });
  const elapsed = useElapsedSeconds(ping.isPending);
  const results = saved ? ping.data : undefined;
  const failed = results?.filter((result) => !result.ok).length ?? 0;
  const detail = (result: ModelPingResult) => {
    const key = categoryMessages[result.category];
    const text = key ? t(key) : result.message;
    return result.status_code ? `${text} (HTTP ${result.status_code})` : text;
  };
  return (
    <div className="space-y-3 border-t border-border px-4 py-3">
      <div className="flex flex-wrap items-center justify-end gap-3">
          {!saved && (
            <p className="text-xs text-muted-foreground">{t("connectivity.saveFirst")}</p>
          )}
          {ping.isPending && elapsed !== undefined && (
            <p className="text-xs tabular-nums text-muted-foreground">
              {t("connectivity.elapsed", { seconds: elapsed })}
            </p>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            title={t("connectivity.description")}
            disabled={!saved || disabled}
            aria-busy={ping.isPending || undefined}
            onClick={() => { if (!ping.isPending) ping.mutate(); }}
          >
            {t("connectivity.test")}
          </Button>
      </div>
      {saved && <ErrorNotice error={ping.error} />}
      {(results != null || ping.isPending) && (
        <div role="status" aria-live="polite" className="space-y-2">
          {results && (
            <p className="text-sm">
              {results.length === 0
                ? t("connectivity.none")
                : failed
                  ? t("connectivity.someFailed", { failed, total: results.length })
                  : t("connectivity.allPassed", { total: results.length })}
            </p>
          )}
          {results && results.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-connectivity text-sm">
                <thead className="text-left text-xs text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-1 pr-3 font-medium">{t("connectivity.model")}</th>
                    <th scope="col" className="py-1 pr-3 font-medium">{t("providerSettings.apiConnection")}</th>
                    <th scope="col" className="py-1 pr-3 font-medium">{t("connectivity.status")}</th>
                    <th scope="col" className="py-1 pr-3 font-medium">{t("connectivity.latency")}</th>
                    <th scope="col" className="py-1 font-medium">{t("connectivity.detail")}</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((result) => (
                    <tr key={result.profile} className="border-t border-subtle align-top">
                      <td className="py-2 pr-3 [overflow-wrap:anywhere]">
                        <div className="font-medium">{result.model}</div>
                      </td>
                      <td className="py-2 pr-3 [overflow-wrap:anywhere]">
                        {result.connection} / {result.provider}
                      </td>
                      <td className="py-2 pr-3">
                        <Badge variant={result.ok ? "success" : "destructive"}>
                          {result.ok ? t("connectivity.passed") : t("connectivity.failed")}
                        </Badge>
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap">
                        {result.latency_ms == null ? "—" : `${result.latency_ms} ms`}
                      </td>
                      <td className="py-2">{detail(result)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
