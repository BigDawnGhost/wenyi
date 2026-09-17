import { useI18n, translate as tr } from "@/i18n";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type EventOut } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice, StructuredData } from "@/components/ui/data";
import { Badge } from "@/components/ui/badge";

function badgeFor(type: string) {
  const BADGE: Record<
    string,
    { variant: "info" | "success" | "warning" | "secondary"; label: string }
  > = {
    project: { variant: "info", label: tr("events.project") },
    run_initialized: { variant: "secondary", label: tr("events.preparation") },
    language_detected: {
      variant: "secondary",
      label: tr("events.preparation"),
    },
    analysis_saved: { variant: "secondary", label: tr("events.preparation") },
    book_synopsis_saved: {
      variant: "secondary",
      label: tr("events.preparation"),
    },
    batch_translated: { variant: "info", label: tr("events.translation") },
    batch_skipped: { variant: "secondary", label: tr("events.translation") },
    chapter_done: { variant: "success", label: tr("events.completed") },
    chapter_reviewed: { variant: "warning", label: tr("common.review") },
    batch_glossary_extracted: {
      variant: "secondary",
      label: tr("common.terms"),
    },
    assembled: { variant: "success", label: tr("common.export") },
  };

  return (
    BADGE[type] || {
      variant: "secondary" as const,
      label: type.split("_")[0] || tr("events.event"),
    }
  );
}

function describe(e: EventOut): string {
  const p = e.payload;
  switch (e.type) {
    case "language_detected":
      return tr("events.sourceLanguageDetected", { language: p.source_lang });
    case "analysis_saved":
      return tr("events.styleAnalysisCompleted");
    case "book_synopsis_saved":
      return tr("events.bookSynopsisGenerated");
    case "batch_translated":
      return tr("events.chapterBatchCompletedParagraphs", {
        chapter: (Number(p.chapter) ?? 0) + 1,
        count: p.count,
      });
    case "batch_skipped":
      return tr("events.chapterTranslatedBatchSkipped", {
        chapter: (Number(p.chapter) ?? 0) + 1,
      });
    case "chapter_done":
      return tr("events.chapterTranslated", {
        chapter: (Number(p.chapter) ?? 0) + 1,
        title: p.title,
      });
    case "chapter_reviewed":
      return tr("events.chapterReviewIssues", {
        chapter: (Number(p.chapter) ?? 0) + 1,
        count: p.issue_count,
      });
    case "assembled":
      return tr("events.exportedFiles", {
        files: (p.outputs as string[])?.join(", ") || "",
      });
    case "run_initialized":
      return tr("events.projectInitializedChapters", { count: p.chapters });
    default:
      return e.type;
  }
}

export default function EventsPage() {
  const { t: tr, locale } = useI18n();
  const { pid = "" } = useParams();
  const { data: events, error } = useQuery({
    queryKey: ["events", pid],
    queryFn: () => api.listEvents(pid),
    select: (items) => [...items].sort((a, b) => b.id - a.id),
    enabled: !!pid,
    refetchInterval: 5000,
  });

  return (
    <>
      <PageHeader
        title={tr("common.eventLog")}
        subtitle={tr("events.keyProjectEventsRefreshedEvery5Seconds")}
      />
      <PageContainer>
        <ErrorNotice error={error} />
        <Card>
          <CardContent className="p-4">
            {!events?.length ? (
              <p className="text-sm text-muted-foreground text-center py-8">
                {tr("events.noEventsYet")}
              </p>
            ) : (
              <div className="space-y-2">
                {events.map((e) => {
                  const b = badgeFor(e.type);
                  return (
                    <div
                      key={e.id}
                      className="flex items-start gap-3 text-sm py-1.5"
                    >
                      <span className="text-xs text-muted-foreground whitespace-nowrap mt-0.5">
                        {e.created_at
                          ? new Date(e.created_at).toLocaleString(locale)
                          : ""}
                      </span>
                      <Badge variant={b.variant}>{b.label}</Badge>
                      <details className="min-w-0 flex-1">
                        <summary className="cursor-pointer break-words">
                          {describe(e)}
                        </summary>
                        <div className="mt-3 rounded border p-3">
                          <StructuredData value={e.payload} />
                        </div>
                      </details>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}
