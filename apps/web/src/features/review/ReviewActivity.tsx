import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useI18n } from "@/i18n";
import type { ProgressMessage } from "@/lib/ws";
import { StatusBadge } from "@/components/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { formatDuration } from "@/features/progress/accountingData";
import { reviewPhase } from "./reviewData";

export function ReviewActivity({
  pid,
  status,
  progress,
}: {
  pid: string;
  status: string;
  progress?: ProgressMessage;
}) {
  const { t, locale } = useI18n();
  const running = status === "running";
  const phase = reviewPhase(progress?.label);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, progress?.run_id]);
  const updated = Date.parse(progress?.updated_at || "");
  const elapsed = progress?.elapsed_seconds;
  const knownElapsed =
    typeof elapsed === "number" && Number.isFinite(elapsed) && elapsed >= 0;
  const seconds = knownElapsed
    ? Math.floor(
        elapsed +
          (running && Number.isFinite(updated)
            ? Math.max(0, now - updated) / 1000
            : 0),
      )
    : undefined;
  const done = progress?.done;
  const total = progress?.total;
  const measurable =
    phase &&
    typeof done === "number" &&
    typeof total === "number" &&
    Number.isFinite(done) &&
    Number.isFinite(total) &&
    total > 0 &&
    done >= 0 &&
    done <= total;
  return (
    <section
      aria-label={t("review.currentRun")}
      className="space-y-4 rounded-lg border p-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          {running && (
            <Loader2
              aria-hidden
              className="h-4 w-4 shrink-0 animate-spin text-muted-foreground"
            />
          )}
          <h2 className="font-medium">
            {phase
              ? t(phase.key, { round: phase.round })
              : t(
                  running || status === "queued"
                    ? "review.waitingProgress"
                    : "review.currentRun",
                )}
          </h2>
        </div>
        <StatusBadge status={status} context="review" />
      </div>
      {measurable && (
        <div className="space-y-2">
          <div className="flex justify-between gap-3 text-sm text-muted-foreground">
            <span>{t("review.stageProgress")}</span>
            <span className="tabular-nums">
              {done.toLocaleString(locale)} / {total.toLocaleString(locale)}
            </span>
          </div>
          <Progress
            role="progressbar"
            aria-label={t("review.stageProgress")}
            aria-valuenow={done}
            aria-valuemin={0}
            aria-valuemax={total}
            value={done}
            max={total}
          />
        </div>
      )}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-xs text-muted-foreground">
        {seconds !== undefined && (
          <span data-testid="review-elapsed">
            {t("review.taskElapsed")}{" "}
            <span className="ml-1 tabular-nums text-foreground">
              {formatDuration(seconds, t)}
            </span>
          </span>
        )}
        {Number.isFinite(updated) && (
          <span>
            {t("review.lastUpdate")}{" "}
            <time dateTime={progress?.updated_at} className="ml-1">
              {new Date(updated).toLocaleString(locale)}
            </time>
          </span>
        )}
        <Link
          to={`/projects/${pid}`}
          className="ml-auto underline underline-offset-4"
        >
          {t("review.openOverview")}
        </Link>
      </div>
    </section>
  );
}
