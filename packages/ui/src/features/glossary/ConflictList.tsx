import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Conflict } from "../../api/types";
import { useI18n } from "../../i18n";
import { toast } from "../../lib/toast";
import { errorMessage } from "../../lib/error-codes";
import { Button } from "../../components/ui/button";
import { Card, CardContent } from "../../components/ui/card";

/** Unresolved terminology conflicts raised by extraction, resolved inline. */
export function ConflictList({
  pid,
  conflicts,
  busy,
  onResolved,
}: {
  pid: string;
  conflicts: Conflict[];
  busy: boolean;
  onResolved: () => void;
}) {
  const { t: tr } = useI18n();
  const resolve = useMutation({
    mutationFn: ({
      cid,
      decision,
      target,
    }: {
      cid: number;
      decision: string;
      target?: string;
    }) => api.resolveConflict(pid, cid, { decision, target }),
    onSuccess: () => {
      onResolved();
      toast.success(tr("glossary.conflictResolved"));
    },
    onError: (e) => toast.error(errorMessage(e, tr)),
  });
  return (
    <Card className="border-amber-300 bg-amber-50 dark:bg-amber-950/20">
      <CardContent className="p-4 space-y-2">
        <div className="font-medium text-sm">
          {tr("glossary.conflictCount", { count: conflicts.length })}
        </div>
        {conflicts.map((c) => (
          <div
            key={c.id}
            className="flex flex-wrap items-center gap-3 text-sm border-t pt-2"
          >
            <span className="font-medium">{c.source}</span>
            <span className="text-muted-foreground">
              {tr("glossary.current")}
              {c.existing_target}
            </span>
            <span className="text-muted-foreground">
              {tr("glossary.aiSuggestion")}
              {c.proposed_target}
            </span>
            <span className="flex gap-1 ml-auto">
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => resolve.mutate({ cid: c.id, decision: "current" })}
              >
                {tr("glossary.keepCurrent")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => resolve.mutate({ cid: c.id, decision: "proposed" })}
              >
                {tr("glossary.acceptSuggestion")}
              </Button>
            </span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
