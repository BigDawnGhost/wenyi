import { useQuery } from "@tanstack/react-query";
import { useI18n } from "@/i18n";
import { api } from "@/lib/api";
import { ErrorNotice } from "@/components/ui/data";
import { Button } from "@/components/ui/button";

export function PrecisionDrafts({
  pid,
  chapterIndex,
  segmentIndex,
  source,
  beforePolish,
  target,
  busy,
}: {
  pid: string;
  chapterIndex: number;
  segmentIndex: number;
  source: string;
  beforePolish: string | null | undefined;
  target: string | null | undefined;
  busy: boolean;
}) {
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["precisionDrafts", pid, chapterIndex, segmentIndex, source, beforePolish, target],
    queryFn: () => api.precisionDrafts(pid, chapterIndex, segmentIndex),
    staleTime: 30_000,
    retry: false,
    refetchInterval: (query) =>
      busy &&
      query.state.data?.available === false &&
      (query.state.data.reason === "no_archive" || query.state.data.reason === "incomplete")
        ? 3000
        : false,
  });
  if (query.isPending)
    return <p role="status">{t("progress.loading")}</p>;
  if (query.isError)
    return (
      <div className="space-y-3">
        <ErrorNotice error={query.error} />
        <Button variant="outline" onClick={() => void query.refetch()}>
          {t("proofreading.retryDrafts")}
        </Button>
      </div>
    );
  const data = query.data;
  if (!data.available)
    return (
      <div className="space-y-3">
        <p role="status" className="rounded-lg border p-4 text-sm text-muted-foreground">
          {t(`proofreading.precisionUnavailable.${data.reason ?? "no_archive"}`)}
        </p>
        <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>
          {t("proofreading.retryDrafts")}
        </Button>
      </div>
    );
  const text = (value: string | null | undefined) => (
    <p className="whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]">
      {value === ""
        ? t("review.emptyTranslation")
        : value ?? t("proofreading.waitingForTranslation")}
    </p>
  );
  return (
    <div className="space-y-4">
      <p className="rounded-lg bg-muted/50 p-3 text-sm">
        {t("proofreading.precisionExplanation")}
      </p>
      {data.candidates.map((candidate) => (
        <section key={candidate.id} className="min-w-0 space-y-3 rounded-lg border p-4">
          <h3 className="flex flex-wrap items-center gap-2 text-sm font-medium">
            {candidate.id}
            {candidate.id === data.before_polish_candidate && (
              <span className="rounded bg-muted px-2 py-1 text-xs">
                {t("proofreading.beforePolishComparison")}
              </span>
            )}
          </h3>
          {text(candidate.target)}
        </section>
      ))}
      <section className="space-y-3 rounded-lg border bg-muted/30 p-4">
        <h3 className="text-sm font-medium">{t("proofreading.archivedSynthesis")}</h3>
        {text(data.synthesized_target)}
      </section>
      <section className="space-y-3 rounded-lg border p-4">
        <h3 className="text-sm font-medium">{t("proofreading.currentTranslation")}</h3>
        {target !== data.synthesized_target && (
          <p className="text-xs text-muted-foreground">{t("proofreading.currentDiffers")}</p>
        )}
        {text(target)}
      </section>
    </div>
  );
}
