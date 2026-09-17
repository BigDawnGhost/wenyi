import { useState } from "react";
import { useI18n, type MessageKey } from "@/i18n";
import { operationLabel } from "@/i18n/labels";
import { cn } from "@/lib/utils";
import {
  amount,
  record,
  tokenParts,
  usageRows,
  type UsageGroup,
} from "./accountingData";

const groups: [UsageGroup, MessageKey][] = [
  ["by_model", "data.byModel"],
  ["by_provider", "data.byProvider"],
  ["by_stage", "data.byStage"],
];
const colors = [
  "bg-sky-600 dark:bg-sky-400",
  "bg-teal-500 dark:bg-teal-400",
  "bg-slate-400",
];

function TokenBar({
  slot,
  scale,
}: {
  slot: Record<string, unknown>;
  scale?: number;
}) {
  const { input, output, other, total } = tokenParts(slot);
  const denominator = Math.max(scale ?? total, input + output + other, 1);
  return (
    <div
      className="flex h-2.5 overflow-hidden rounded-full bg-muted"
      aria-hidden="true"
    >
      {[input, output, other].map((value, index) => (
        <span
          key={index}
          className={colors[index]}
          style={{ width: `${(value / denominator) * 100}%` }}
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
  const [group, setGroup] = useState<UsageGroup>("by_model");
  const totals = record(usage.totals);
  const labels = record(usage.labels);
  const rows = usageRows(usage, group);
  const maximum = Math.max(
    ...rows.map(({ slot }) => tokenParts(slot).total),
    1,
  );
  const number = (value: unknown) =>
    amount(value)?.toLocaleString(locale) ?? "—";
  const parts = tokenParts(totals);
  const legend: [MessageKey, unknown][] = [
    ["data.inputTokens", totals.prompt_tokens],
    ["data.outputTokens", totals.completion_tokens],
    ...(parts.other
      ? [
          ["accounting.unclassifiedTokens", parts.other] as [
            MessageKey,
            unknown,
          ],
        ]
      : []),
  ];
  return (
    <section
      aria-label={t("accounting.tokenUsage")}
      className="min-w-0 space-y-5"
    >
      <h4 className="text-sm font-medium">{t("accounting.tokenUsage")}</h4>
      <figure
        aria-label={t("accounting.tokenComposition")}
        className="space-y-3"
      >
        <TokenBar slot={totals} />
        <figcaption className="flex flex-wrap gap-x-5 gap-y-2 text-xs">
          {legend.map(([label, value], index) => (
            <span
              key={label}
              className="inline-flex items-center gap-2 text-muted-foreground"
            >
              <span
                className={cn("h-2 w-2 shrink-0 rounded-full", colors[index])}
                aria-hidden="true"
              />
              {t(label)}{" "}
              <span className="font-medium text-foreground tabular-nums">
                {number(value)}
              </span>
            </span>
          ))}
        </figcaption>
      </figure>
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
            const label =
              typeof labels[id] === "string"
                ? String(labels[id])
                : group === "by_stage"
                  ? operationLabel(id, t)
                  : id;
            return (
              <li key={id} className="space-y-2">
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-sm">
                  <span className="min-w-0 break-words font-medium [overflow-wrap:anywhere]">
                    {label}
                  </span>
                  <span className="shrink-0 tabular-nums">
                    {number(slot.total_tokens)}{" "}
                    <span className="text-xs text-muted-foreground">
                      tokens
                    </span>
                  </span>
                </div>
                <TokenBar slot={slot} scale={maximum} />
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground tabular-nums">
                  <span>
                    {t("accounting.callCount", { count: number(slot.calls) })}
                  </span>
                  <span>
                    {t("data.inputTokens")}: {number(slot.prompt_tokens)}
                  </span>
                  <span>
                    {t("data.outputTokens")}: {number(slot.completion_tokens)}
                  </span>
                  {amount(slot.cache_hit_tokens) !== undefined && (
                    <span>
                      {t("data.cachedTokens")}: {number(slot.cache_hit_tokens)}
                    </span>
                  )}
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
