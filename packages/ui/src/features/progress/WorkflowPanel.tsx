import { useId } from "react";
import { StatusBadge } from "../../components/StatusBadge";
import { useI18n } from "../../i18n";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { ProgressMessage } from "../../lib/ws";
import { ErrorNotice } from "../../components/ui/data";
import { cardSurface } from "../../components/ui/card";
import { cn } from "../../lib/utils";
import { progressInterval } from "../../lib/runtime";
import { workflowStageLabel } from "../../i18n/labels";

export function WorkflowPanel({
  pid,
  msg,
  failure,
  connected = false,
  busy,
}: {
  pid: string;
  msg: ProgressMessage | null;
  /** The run's terminal failure, kept apart from the stage label it outlives. */
  failure?: ProgressMessage | null;
  connected?: boolean;
  busy: boolean;
}) {
  const { t: tr } = useI18n();
  const headingId = useId();
  const TASKS: Record<string, string> = {
    parse: tr("workflowPanel.sourceParsing"),
    prepare: tr("common.preparation"),
    translation: tr("workflowPanel.bookTranslation"),
    chapter_translation: tr("workflowPanel.chapterTranslation"),
    review: tr("common.wholeBookReview"),
  };

  const query = useQuery({
    queryKey: ["workflow", pid],
    queryFn: () => api.getWorkflow(pid),
    refetchInterval: busy ? progressInterval(connected, 2500) : false,
  });
  const workflow = query.data;
  const cached = workflow?.progress;
  const cachedIsNewer =
    typeof cached?.updated_at === "string" &&
    !!msg?.updated_at &&
    Date.parse(cached.updated_at) > Date.parse(msg.updated_at);
  const stage =
    msg?.run_id &&
    msg.run_id === workflow?.run_id &&
    msg.label &&
    !cachedIsNewer
      ? msg
      : cached;
  const active = workflow?.status === "running";
  const live = active || workflow?.status === "paused" || workflow?.status === "error"
    ? stage
    : null;

  return (
    <section aria-labelledby={headingId} className={cn(cardSurface, "space-y-3 p-4")}>
      <h2 id={headingId} className="font-medium">
        {tr("workflowPanel.currentWorkflow")}
      </h2>
      <ErrorNotice error={query.error} />
      {/* The run's terminal failure arrives as a socket frame, so this is the
          only place its code and message can still be read. The stage label
          below is deliberately left as it was. */}
      {failure && <ErrorNotice error={failure} />}
      {/* The card is always here. Rendering it only once the workflow arrives made
          the whole block appear under the page content a moment after it settled. */}
      <div role="status" aria-atomic="true" className="space-y-2 text-sm">
        {workflow ? (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <span className="font-medium">{TASKS[workflow.kind] || workflow.kind}</span>
              <StatusBadge status={workflow.status} />
            </div>
            {/* The row is always present. Hiding it when a run is paused or has
                no stage yet collapsed the card by a line, so the page shifted
                every time the workflow changed state. The placeholder carries no
                counts: inventing a "0 / 0" to fill the space would be worse than
                the empty line. */}
            <p className="flex min-h-5 flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {live?.label
                  ? workflowStageLabel(String(live.label), String(live.label), tr)
                  : active ? tr("workflowPanel.waitingForProgressUpdates") : null}
              </span>
              {!!live?.label && Number(live.total) > 0 && (
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {tr("workflow.stageCount", {
                    done: Number(live.done) || 0,
                    total: Number(live.total),
                  })}
                </span>
              )}
            </p>
          </>
        ) : (
          // Placeholder rather than nothing, so the card does not grow a row when
          // the workflow arrives.
          <span className="text-muted-foreground">{tr("workflowPanel.waitingForProgressUpdates")}</span>
        )}
      </div>
    </section>
  );
}
