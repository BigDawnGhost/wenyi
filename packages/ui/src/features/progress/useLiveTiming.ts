import { useEffect, useMemo, useState } from "react";
import { amount, record, type Stats } from "./accountingData";

export function useLiveTiming(value: Stats | undefined, running: boolean) {
  const live = value?.live;
  const key = live ? `${live.run_id}:${live.updated_at}` : "";
  // Repeated polling of the same Redis snapshot must not restart the local clock.
  const anchor = useMemo(
    () => ({
      started: performance.now(),
      validFor: amount(live?.valid_for_seconds) ?? 0,
    }),
    [key],
  );
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!key || !running) return;
    setNow(performance.now());
    const timer = window.setInterval(() => setNow(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, [key, running]);
  const timing = record(value?.timing);
  if (!live || !Array.isArray(timing.runs)) return timing;
  const elapsed = Math.min(
    Math.max(0, Math.floor((now - anchor.started) / 1000)),
    anchor.validFor,
  );
  let increment = 0;
  const runs = timing.runs.map((item) => {
    const run = record(item);
    if (run.status !== "running") return run;
    increment += elapsed;
    return {
      ...run,
      elapsed_seconds: (amount(run.elapsed_seconds) ?? 0) + elapsed,
    };
  });
  return {
    ...timing,
    runs,
    total_seconds: (amount(timing.total_seconds) ?? 0) + increment,
  };
}
