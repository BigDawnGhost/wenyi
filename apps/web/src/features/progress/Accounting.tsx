import {
  BarChart3,
  Clock3,
  Database,
  Layers,
  MessagesSquare,
} from "lucide-react";
import { lazy, Suspense } from "react";
import { useI18n } from "@/i18n";
import {
  amount,
  cacheRate,
  formatDuration,
  record,
  type Stats,
} from "./accountingData";

const UsageChart = lazy(() => import("./UsageChart"));
const RunTimeChart = lazy(() => import("./RunTimeChart"));

export function Accounting({ value }: { value?: Stats }) {
  const { t, locale } = useI18n();
  if (!value)
    return (
      <p className="text-sm text-muted-foreground">
        {t("progress.noUsageRecordedYet")}
      </p>
    );
  const usage = record(value.usage);
  const totals = record(usage.totals);
  const timing = record(value.timing);
  const number = (value: unknown) =>
    amount(value)?.toLocaleString(locale) ?? "—";
  const rate = cacheRate(totals);
  const metrics = [
    {
      label: t("progress.cumulativeTokens"),
      value: number(totals.total_tokens),
      icon: Layers,
    },
    {
      label: t("accounting.requests"),
      value: number(totals.calls),
      icon: MessagesSquare,
    },
    {
      label: t("data.cacheHitRate"),
      value:
        rate === undefined
          ? "—"
          : new Intl.NumberFormat(locale, {
              style: "percent",
              maximumFractionDigits: 1,
            }).format(rate),
      icon: Database,
    },
    {
      label: t("accounting.recordedTime"),
      value: formatDuration(timing.total_seconds, t),
      icon: Clock3,
    },
  ];
  return (
    <section aria-label={t("progress.totalUsageRunTime")} className="space-y-5">
      <dl className="grid grid-cols-2 gap-5 rounded-lg bg-muted/40 p-4 lg:grid-cols-4">
        {metrics.map(({ label, value, icon: Icon }) => (
          <div key={label} className="min-w-0">
            <dt className="flex items-center gap-2 text-xs text-muted-foreground">
              <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              {label}
            </dt>
            <dd className="mt-2 break-words text-lg font-semibold tracking-tight tabular-nums sm:text-xl">
              {value}
            </dd>
          </div>
        ))}
      </dl>
      <div className="border-t pt-4">
        <h3 className="flex items-center gap-2 text-sm font-medium">
          <BarChart3
            className="h-4 w-4 text-muted-foreground"
            aria-hidden="true"
          />
          {t("accounting.details")}
        </h3>
        <Suspense
          fallback={
            <p className="mt-5 text-sm text-muted-foreground">
              {t("progress.loading")}
            </p>
          }
        >
          <div className="mt-5 grid min-w-0 gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <UsageChart usage={usage} />
            <RunTimeChart timing={timing} />
          </div>
        </Suspense>
      </div>
    </section>
  );
}
