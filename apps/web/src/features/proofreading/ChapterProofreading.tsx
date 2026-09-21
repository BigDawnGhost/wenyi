import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useI18n } from "@/i18n";
import { api, type ChapterSummary } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice, StructuredData } from "@/components/ui/data";
import { Disclosure } from "@/components/ui/disclosure";
import { ParagraphActions } from "./ParagraphActions";
import { ParagraphEditor } from "./ParagraphEditor";

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
  const [searchParams] = useSearchParams();
  const requested = searchParams.get("segment");
  const focused = useRef<string | undefined>(undefined);
  const [editor, setEditor] = useState<{
    index: number;
    view: "edit" | "history";
  } | null>(null);
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
  const activeSegment = segments.find((s) => s.index === editor?.index);
  useEffect(() => {
    if (!requested || !/^\d+$/.test(requested)) {
      focused.current = undefined;
      return;
    }
    if (focused.current === requested || !chapter.data) return;
    const row = document.getElementById(`paragraph-${Number(requested)}`);
    if (row) {
      row.scrollIntoView({ block: "center" });
      row.focus({ preventScroll: true });
      focused.current = requested;
    }
  }, [requested, chapter.data]);
  return (
    <>
      <PageHeader
        title={t("review.manualProofreading", {
          title:
            chapter.data?.title_translated?.trim() ||
            chapter.data?.title.trim() ||
            t(chapter.data ? "common.untitledChapter" : "progress.loading"),
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
            {segments.map((segment) => (
              <div
                key={segment.index}
                id={`paragraph-${segment.index}`}
                tabIndex={-1}
                className="grid lg:grid-cols-2 border-b last:border-0 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-ring/30 focus:bg-muted/30"
              >
                <div className="min-w-0 p-4 text-sm lg:border-r">
                  <span className="mb-2 block text-xs text-muted-foreground lg:hidden">
                    {t("common.source")}
                  </span>
                  <p className="whitespace-pre-wrap leading-relaxed [overflow-wrap:anywhere]">
                    {segment.source}
                  </p>
                </div>
                <ParagraphActions
                  source={segment.source}
                  target={segment.target}
                  disabled={readOnly || chapter.isError}
                  onOpen={(view) => setEditor({ index: segment.index, view })}
                />
              </div>
            ))}
          </CardContent>
        </Card>
        {editor && activeSegment && (
          <ParagraphEditor
            key={activeSegment.index}
            pid={pid}
            chapterIndex={index}
            segment={activeSegment}
            initialView={editor.view}
            readOnly={readOnly || chapter.isError}
            onClose={() => setEditor(null)}
          />
        )}
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
