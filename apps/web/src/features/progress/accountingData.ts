import type { createTranslator } from "@/i18n/catalog";

export type Translator = ReturnType<typeof createTranslator>;
export type UsageGroup = "by_model" | "by_provider" | "by_stage";
export type Stats = {
  usage?: Record<string, unknown>;
  timing?: Record<string, unknown>;
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
  const hit = amount(slot.cache_hit_tokens);
  const miss = amount(slot.cache_miss_tokens);
  const input = amount(slot.prompt_tokens);
  if (
    hit === undefined ||
    miss === undefined ||
    hit + miss === 0 ||
    (input !== undefined && hit + miss !== input)
  )
    return undefined;
  return hit / (hit + miss);
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
  const cached = amount(slot.cache_hit_tokens);
  const uncached = amount(slot.cache_miss_tokens);
  const reported = (cached ?? 0) + (uncached ?? 0);
  // Unreported cache usage cannot be inferred to be a cache miss.
  const validCache = reported <= input;
  return {
    input,
    output,
    other: Math.max(0, total - input - output),
    total,
    cachedInput: validCache ? cached : undefined,
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
    const slot = record(value);
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
    ]) {
      const left = amount(existing.slot[field]);
      const right = amount(slot[field]);
      existing.slot[field] =
        left === undefined || right === undefined ? undefined : left + right;
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
