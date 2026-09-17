import { useState } from "react";
import { useI18n, type MessageKey } from "@/i18n";
import { operationLabel } from "@/i18n/labels";
import { cn } from "@/lib/utils";
import {
  amount,
  cacheRate,
  record,
  tokenParts,
  usageRows,
  type UsageGroup,
} from "./accountingData";

const groups: [UsageGroup, MessageKey][] = [
  ["by_stage", "data.byStage"],
  ["by_model", "data.byModel"],
  ["by_provider", "data.byProvider"],
];
type TokenPart = {
  label: MessageKey;
  value: number | undefined;
  color: string;
};

function tokenBreakdown(slot: Record<string, unknown>): TokenPart[] {
  const parts = tokenParts(slot);
  return [
    {
      label: "accounting.cachedInput",
      value: parts.cachedInput,
      color: "bg-sky-300",
    },
    {
      label: "accounting.uncachedInput",
      value: parts.uncachedInput,
      color: "bg-sky-600 dark:bg-sky-400",
    },
    ...(parts.unknownInput
      ? [
          {
            label: "accounting.unknownInput" as const,
            value: parts.unknownInput,
            color: "bg-slate-300",
          },
        ]
      : []),
    {
      label: "data.outputTokens",
      value: amount(slot.completion_tokens),
      color: "bg-teal-500 dark:bg-teal-400",
    },
    ...(parts.other
      ? [
          {
            label: "accounting.unclassifiedTokens" as const,
            value: parts.other,
            color: "bg-slate-500",
          },
        ]
      : []),
  ];
}

function TokenBar({ parts, scale }: { parts: TokenPart[]; scale: number }) {
  const denominator = Math.max(
    scale,
    parts.reduce((sum, part) => sum + (part.value ?? 0), 0),
    1,
  );
  return (
    <div
      className="flex h-2.5 overflow-hidden rounded-full bg-muted"
      aria-hidden="true"
    >
      {parts.map(({ label, value, color }) => (
        <span
          key={label}
          className={color}
          style={{ width: `${((value ?? 0) / denominator) * 100}%` }}
        />
      ))}
    </div>
  );
}

export default function UsageChart({
  usage,
}: {
  usage: Record<string, unknown>;
}) {
  const { t, locale } = useI18n();
  const [group, setGroup] = useState<UsageGroup>(groups[0][0]);
  const labels = record(usage.labels);
  const rows = usageRows(usage, group);
  const maximum = Math.max(
    ...rows.map(({ slot }) => tokenParts(slot).total),
    1,
  );
  const number = (value: unknown) =>
    amount(value)?.toLocaleString(locale) ?? "—";
  const percent = new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 1,
  });
  return (
    <section
      aria-label={t("accounting.usageBreakdown")}
      className="min-w-0 space-y-5"
    >
      <div
        className="flex flex-wrap gap-1 rounded-lg bg-muted/60 p-1"
        role="group"
        aria-label={t("accounting.groupBy")}
      >
        {groups.map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={key === group}
            onClick={() => setGroup(key)}
            className={cn(
              "min-w-0 flex-1 rounded-md px-3 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              key === group
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t(label)}
          </button>
        ))}
      </div>
      {rows.length ? (
        <ul
          aria-label={t(groups.find(([key]) => key === group)![1])}
          className="max-h-96 space-y-5 overflow-y-auto pr-1"
        >
          {rows.map(({ id, slot }) => {
            const parts = tokenBreakdown(slot);
            const rate = cacheRate(slot);
            const label =
              typeof labels[id] === "string"
                ? String(labels[id])
                : group === "by_stage"
                  ? operationLabel(id, t)
                  : id;
            return (
              <li key={id} className="space-y-2">
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-sm">
                  <span className="min-w-0 font-medium [overflow-wrap:anywhere]">
                    {label}
                  </span>
                  <span className="shrink-0 tabular-nums">
                    {number(slot.total_tokens)}{" "}
                    <span className="text-xs text-muted-foreground">
                      tokens
                    </span>
                  </span>
                </div>
                <TokenBar parts={parts} scale={maximum} />
                <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-muted-foreground tabular-nums">
                  {parts.map(({ label, value, color }) => (
                    <span
                      key={label}
                      className="inline-flex items-center gap-1.5"
                    >
                      <span
                        className={cn("h-2 w-2 shrink-0 rounded-full", color)}
                        aria-hidden="true"
                      />
                      {t(label)}: {number(value)}
                    </span>
                  ))}
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground tabular-nums">
                  <span>
                    {t("accounting.callCount", { count: number(slot.calls) })}
                  </span>
                  <span>
                    {t("data.cacheHitRate")}:{" "}
                    {rate === undefined ? "—" : percent.format(rate)}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {t("accounting.noBreakdown")}
        </p>
      )}
    </section>
  );
}
