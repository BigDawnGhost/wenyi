import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useI18n } from "../../i18n";
import { api } from "../../api/client";
import type { ChapterSummary } from "../../api/types";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Button } from "../../components/ui/button";
import { ErrorNotice, ReadOnlyNotice, StructuredData } from "../../components/ui/data";
import { Disclosure } from "../../components/ui/disclosure";
import { VirtualChapter } from "./VirtualChapter";
import { WINDOW_SIZE, windowKey } from "./useChapterWindows";

export function ChapterProofreading({
  pid, index, chapters, busy, readOnly, sourceOnly, error, targetLanguage,
}: {
  pid: string;
  index: number;
  chapters: ChapterSummary[];
  busy: boolean;
  readOnly: boolean;
  sourceOnly: boolean;
  error: unknown;
  targetLanguage?: string;
}) {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const requested = params.get("segment");
  const around = requested && /^\d+$/.test(requested) && Number.isSafeInteger(Number(requested))
    ? Number(requested) : undefined;
  const validIndex = Number.isSafeInteger(index) && index >= 0;
  const chapter = useQuery({
    queryKey: [...windowKey(pid, index, sourceOnly), "initial", around],
    queryFn: ({ signal }) => api.chapterWindow(pid, index, 0, WINDOW_SIZE, around, signal),
    enabled: validIndex,
    staleTime: Infinity,
    gcTime: 0,
  });
  const current = chapters.findIndex((c) => c.index === index);
  const previous = current > 0 ? chapters[current - 1] : undefined;
  const next = current >= 0 ? chapters[current + 1] : undefined;
  const summary = chapters[current];
  return (
    <>
      <PageHeader title={t("review.manualProofreading", {
        title: chapter.data?.title_translated?.trim() || chapter.data?.title.trim()
          || t(chapter.data ? "common.untitledChapter" : "progress.loading"),
      })} actions={
        <>
          <Link to={`/projects/${pid}/proofreading`}>
            <Button variant="outline">{t("review.proofreadByChapter")}</Button>
          </Link>
          {previous && <Link to={`/projects/${pid}/proofreading/${previous.index}`}>
            <Button variant="outline">{t("review.previousChapter")}</Button>
          </Link>}
          {next && <Link to={`/projects/${pid}/proofreading/${next.index}`}>
            <Button variant="outline">{t("review.nextChapter")}</Button>
          </Link>}
        </>
      } />
      <PageContainer className="space-y-4">
        <ErrorNotice error={error || chapter.error
          || (!validIndex ? new Error(t("proofreading.invalidChapter")) : undefined)} />
        {sourceOnly ? <ReadOnlyNotice reason="source" /> : busy && <ReadOnlyNotice />}
        {!sourceOnly && chapter.data && (
          <p className="text-sm text-muted-foreground">
            {t("proofreading.savedParagraphs", {
              done: summary ? summary.target_word_count : chapter.data.saved_count,
              total: chapter.data.total,
            })}
          </p>
        )}
        {chapter.data && (
          <VirtualChapter pid={pid} ci={index} initial={chapter.data} sourceOnly={sourceOnly}
            busy={busy} readOnly={readOnly || chapter.isError} targetLanguage={targetLanguage} />
        )}
        {!sourceOnly && (
          <Disclosure lazy title={t("review.recordedReviewNotesForThisChapter")}>
            <ChapterIssues pid={pid} ci={index} busy={busy} />
          </Disclosure>
        )}
      </PageContainer>
    </>
  );
}

function ChapterIssues({ pid, ci, busy }: { pid: string; ci: number; busy: boolean }) {
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["chapterIssues", pid, ci],
    queryFn: () => api.chapterIssues(pid, ci),
    gcTime: 0,
    refetchInterval: busy ? 3000 : false,
  });
  return <>
    <ErrorNotice error={query.error} />
    <StructuredData value={query.data} empty={t("review.noNotesRecordedCheckTheWholeBook")} />
  </>;
}
