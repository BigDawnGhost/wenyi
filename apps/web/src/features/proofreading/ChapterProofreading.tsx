import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useI18n } from "@/i18n";
import { api, type ChapterSummary } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice, StructuredData } from "@/components/ui/data";
import { Disclosure } from "@/components/ui/disclosure";
import { SegmentEditor } from "@/components/SegmentEditor";

export function ChapterProofreading({
  pid,
  index,
  chapters,
  busy,
  readOnly,
  error,
}: {
  pid: string;
  index: number;
  chapters: ChapterSummary[];
  busy: boolean;
  readOnly: boolean;
  error: unknown;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const validIndex = Number.isSafeInteger(index) && index >= 0;
  const chapter = useQuery({
    queryKey: ["review", pid, index],
    queryFn: () => api.getReview(pid, index),
    enabled: validIndex,
    refetchInterval: 3000,
  });
  const current = chapters.findIndex((c) => c.index === index);
  const previous = current > 0 ? chapters[current - 1] : undefined;
  const next = current >= 0 ? chapters[current + 1] : undefined;
  const segments = chapter.data?.segments.filter((s) => s.source.trim()) || [];
  const paragraphs = segments.filter((s) => s.kind === "text");
  const saved = paragraphs.filter((s) => s.target != null).length;
  return (
    <>
      <PageHeader
        title={t("review.manualProofreading", {
          title:
            chapter.data?.title_translated ||
            chapter.data?.title ||
            t("progress.loading"),
        })}
        subtitle={t("proofreading.savedBatchesRefresh")}
        actions={
          <>
            <Link to={`/projects/${pid}/proofreading`}>
              <Button variant="outline">
                {t("review.proofreadByChapter")}
              </Button>
            </Link>
            {previous && (
              <Link to={`/projects/${pid}/proofreading/${previous.index}`}>
                <Button variant="outline">{t("review.previousChapter")}</Button>
              </Link>
            )}
            {next && (
              <Link to={`/projects/${pid}/proofreading/${next.index}`}>
                <Button variant="outline">{t("review.nextChapter")}</Button>
              </Link>
            )}
          </>
        }
      />
      <PageContainer className="space-y-4">
        <ErrorNotice
          error={
            error ||
            chapter.error ||
            (!validIndex
              ? new Error(t("proofreading.invalidChapter"))
              : undefined)
          }
        />
        {busy && (
          <p role="status" className="rounded border p-3 text-sm">
            {t("proofreading.pauseToEdit")}
          </p>
        )}
        {chapter.data && (
          <p className="text-sm text-muted-foreground">
            {t("proofreading.savedParagraphs", {
              done: saved,
              total: paragraphs.length,
            })}
          </p>
        )}
        <Card>
          <CardContent className="p-0">
            <div className="grid grid-cols-2 border-b p-3 text-sm font-medium">
              <span>{t("common.source")}</span>
              <span>{t("common.translation")}</span>
            </div>
            {segments.map((segment) => (
              <div
                key={segment.index}
                className="grid md:grid-cols-2 border-b last:border-0"
              >
                <div className="p-3 whitespace-pre-wrap text-sm border-r">
                  <span className="text-xs text-muted-foreground mr-2">
                    #{segment.index + 1}
                  </span>
                  {segment.source}
                </div>
                <div>
                  {segment.target == null ? (
                    <p className="p-3 text-sm text-muted-foreground">
                      {t("proofreading.waitingForTranslation")}
                    </p>
                  ) : (
                    <SegmentEditor
                      value={segment.target}
                      disabled={readOnly || chapter.isError}
                      onSave={async (target) => {
                        await api.editSegment(
                          pid,
                          index,
                          segment.index,
                          target,
                        );
                        await Promise.all([
                          qc.invalidateQueries({
                            queryKey: ["review", pid, index],
                          }),
                          qc.invalidateQueries({ queryKey: ["chapters", pid] }),
                        ]);
                      }}
                    />
                  )}
                  {segment.target_before_polish && (
                    <details className="px-3 pb-3 text-sm">
                      <summary className="text-muted-foreground cursor-pointer">
                        {t("review.translationBeforePolishing")}
                      </summary>
                      <p className="mt-2 whitespace-pre-wrap">
                        {segment.target_before_polish}
                      </p>
                    </details>
                  )}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Disclosure title={t("review.recordedReviewNotesForThisChapter")}>
          <StructuredData
            value={chapter.data?.review_issues}
            empty={t("review.noNotesRecordedCheckTheWholeBook")}
          />
        </Disclosure>
      </PageContainer>
    </>
  );
}
