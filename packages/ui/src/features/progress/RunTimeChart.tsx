import { useState } from "react";
import { useI18n } from "@/i18n";
import { operationLabel } from "@/i18n/labels";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { formatDuration, timingRuns } from "./accountingData";

export default function RunTimeChart({
  timing,
}: {
  timing: Record<string, unknown>;
}) {
  const { t, locale } = useI18n();
  const [showAll, setShowAll] = useState(false);
  const runs = timingRuns(timing);
  const maximum = Math.max(...runs.map((run) => run.seconds ?? 0), 1);
  const date = (value: string) => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
      ? "—"
      : new Intl.DateTimeFormat(locale, {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        }).format(parsed);
  };
  return (
    <section
      aria-label={t("accounting.runHistory")}
      className="min-w-0 border-t pt-5 xl:border-l xl:border-t-0 xl:pl-6 xl:pt-0"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <h4 className="text-sm font-medium">{t("accounting.runHistory")}</h4>
        <span className="text-xs text-muted-foreground">
          {t("review.runCount", { count: runs.length })}
        </span>
      </div>
      <p className="mb-5 text-xs leading-relaxed text-muted-foreground">
        {t("accounting.timeHelp")}
      </p>
      {runs.length ? (
        <ol
          aria-label={t("accounting.runHistory")}
          className="max-h-96 space-y-5 overflow-y-auto pr-1"
        >
          {(showAll ? runs : runs.slice(0, 3)).map((run) => (
            <li key={run.id} className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="min-w-0 break-words font-medium [overflow-wrap:anywhere]">
                  {operationLabel(run.operation, t)}
                </span>
                <span className="shrink-0 font-medium tabular-nums">
                  {formatDuration(run.seconds, t)}
                </span>
              </div>
              <div
                className="h-1.5 overflow-hidden rounded-full bg-muted"
                aria-hidden="true"
              >
                <div
                  className="h-full rounded-full bg-slate-500 dark:bg-slate-400"
                  style={{ width: `${((run.seconds ?? 0) / maximum) * 100}%` }}
                />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                <time dateTime={run.startedAt || undefined}>
                  {date(run.startedAt)}
                </time>
                <StatusBadge status={run.status} />
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {t("accounting.noRuns")}
        </p>
      )}
      {runs.length > 3 && (
        <Button
          variant="ghost"
          size="sm"
          className="mt-4 w-full"
          onClick={() => setShowAll(!showAll)}
        >
          {t(showAll ? "accounting.showRecentRuns" : "accounting.showAllRuns", {
            count: runs.length,
          })}
        </Button>
      )}
    </section>
  );
}
