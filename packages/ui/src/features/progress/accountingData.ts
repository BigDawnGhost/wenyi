import type { createTranslator } from "@/i18n/catalog";

export type Translator = ReturnType<typeof createTranslator>;
export type UsageGroup = "by_model" | "by_provider" | "by_stage";
export type Stats = {
  usage?: Record<string, unknown>;
  timing?: Record<string, unknown>;
  live?: {
    run_id: string;
    updated_at: string;
    valid_for_seconds: number;
  } | null;
};

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function amount(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}

export function cacheRate(slot: Record<string, unknown>) {
  const parts = tokenParts(slot);
  const input = amount(slot.prompt_tokens);
  if (!input || parts.cachedInput === undefined) return undefined;
  return parts.cachedInput / input;
}

export function reportedAmount(slot: Record<string, unknown>, field: string) {
  const value = amount(slot[field]);
  const presence = amount(slot[field.replace(/_tokens$/, "_reported_calls")]);
  return presence === 0 && !value ? undefined : value;
}

export function formatDuration(value: unknown, t: Translator) {
  const duration = amount(value);
  if (duration === undefined) return "—";
  if (duration < 60)
    return t("progress.seconds", { seconds: Math.round(duration * 10) / 10 });
  const hours = Math.floor(duration / 3600);
  const minutes = Math.floor((duration % 3600) / 60);
  const seconds = Math.floor(duration % 60);
  return hours
    ? t("accounting.hoursMinutesSeconds", { hours, minutes, seconds })
    : t("accounting.minutesSeconds", { minutes, seconds });
}

export function tokenParts(slot: Record<string, unknown>) {
  const input = amount(slot.prompt_tokens) ?? 0;
  const output = amount(slot.completion_tokens) ?? 0;
  const total = amount(slot.total_tokens) ?? input + output;
  const legacy = slot.cache_unknown_tokens === undefined;
  let cached = reportedAmount(slot, "cache_hit_tokens");
  const miss = amount(slot.cache_miss_tokens) ?? 0;
  if (legacy && !cached && !amount(slot.cache_hit_reported_calls)) cached = undefined;
  const written = reportedAmount(slot, "cache_write_tokens");
  const uncached = reportedAmount(slot, "uncached_tokens");
  const reported = (cached ?? 0) + (written ?? 0) + (uncached ?? 0);
  // Historical misses cannot be split into cache writes and ordinary input.
  const validCache = reported <= input && (!legacy || (cached ?? 0) + miss <= input);
  return {
    input,
    output,
    other: Math.max(0, total - input - output),
    total,
    cachedInput: validCache ? cached : undefined,
    writtenInput: validCache ? written : undefined,
    uncachedInput: validCache ? uncached : undefined,
    unknownInput: validCache ? input - reported : input,
  };
}

export function usageRows(usage: Record<string, unknown>, group: UsageGroup) {
  const labels = record(usage.labels);
  const rows = new Map<string, { id: string; slot: Record<string, unknown> }>();
  for (const [id, value] of Object.entries(record(usage[group]))) {
    const label = typeof labels[id] === "string" ? labels[id].trim() : "";
    // A model's ledger identity also includes inference options and output limits.
    // Combine matching provider/model names only in this presentation view.
    const key = group === "by_model" && label ? `model:${label}` : `id:${id}`;
    const original = record(value);
    const parts = tokenParts(original);
    const slot: Record<string, unknown> = {
      ...original,
      cache_hit_tokens: parts.cachedInput,
      cache_write_tokens: parts.writtenInput,
      uncached_tokens: parts.uncachedInput,
      cache_unknown_tokens: parts.unknownInput,
    };
    const existing = rows.get(key);
    if (!existing) {
      rows.set(key, { id, slot: { ...slot } });
      continue;
    }
    for (const field of [
      "calls",
      "prompt_tokens",
      "completion_tokens",
      "total_tokens",
      "cache_hit_tokens",
      "cache_miss_tokens",
      "cache_write_tokens",
      "uncached_tokens",
      "cache_unknown_tokens",
      "cache_write_5m_tokens",
      "cache_write_1h_tokens",
      "cache_hit_reported_calls",
      "cache_write_reported_calls",
      "uncached_reported_calls",
      "cache_write_5m_reported_calls",
      "cache_write_1h_reported_calls",
    ]) {
      const left = amount(existing.slot[field]);
      const right = amount(slot[field]);
      const partialCounter = field.startsWith("cache_") || field.startsWith("uncached_");
      existing.slot[field] =
        (left === undefined && right === undefined) ||
        (!partialCounter && (left === undefined || right === undefined))
          ? undefined
          : (left ?? 0) + (right ?? 0);
    }
    delete existing.slot.cache_hit_rate;
  }
  return [...rows.values()].sort(
    (a, b) =>
      tokenParts(b.slot).total - tokenParts(a.slot).total ||
      a.id.localeCompare(b.id),
  );
}

export function timingRuns(timing: Record<string, unknown>) {
  if (!Array.isArray(timing.runs)) return [];
  return timing.runs
    .map(record)
    .filter((run) => typeof run.id === "string")
    .map((run) => ({
      id: String(run.id),
      operation: typeof run.operation === "string" ? run.operation : "",
      status: typeof run.status === "string" ? run.status : "",
      startedAt: typeof run.started_at === "string" ? run.started_at : "",
      seconds: amount(run.elapsed_seconds),
    }))
    .sort(
      (a, b) => (Date.parse(b.startedAt) || 0) - (Date.parse(a.startedAt) || 0),
    );
}
