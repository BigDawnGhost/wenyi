import { Link, Navigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useI18n } from "@/i18n";
import { projectStatusLabel } from "@/i18n/labels";
import { api, isProjectBusy } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ErrorNotice } from "@/components/ui/data";
import { ChapterProofreading } from "./ChapterProofreading";

export default function ProofreadingPage() {
  const { t } = useI18n();
  const { pid = "", ci } = useParams();
  const project = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
  });
  const subtitle = project.data?.fmt === "srt";
  const chapters = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
    enabled: !!project.data && !subtitle,
    refetchInterval: 3000,
  });
  const busy = isProjectBusy(project.data?.status);
  if (subtitle) return <Navigate to={`/projects/${pid}/subtitles`} replace />;
  if (ci !== undefined) {
    return (
      <ChapterProofreading
        key={`${pid}/${ci}`}
        pid={pid}
        index={Number(ci)}
        chapters={chapters.data || []}
        busy={busy}
        readOnly={busy || !project.data || project.isError || chapters.isError}
        error={project.error || chapters.error}
      />
    );
  }
  return (
    <>
      <PageHeader
        title={t("progress.manualProofreading")}
        subtitle={t("proofreading.savedBatchesRefresh")}
      />
      <PageContainer className="space-y-4">
        <ErrorNotice error={project.error || chapters.error} />
        <Card>
          <CardContent className="p-4 space-y-3">
            <h2 className="font-medium">{t("review.proofreadByChapter")}</h2>
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {chapters.data?.map((chapter) => (
                <Link
                  key={chapter.index}
                  className="rounded border p-3 space-y-2 hover:bg-accent"
                  to={`/projects/${pid}/proofreading/${chapter.index}`}
                >
                  <div className="text-sm font-medium">
                    {chapter.title_translated || chapter.title}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {t("proofreading.savedParagraphs", {
                      done: chapter.target_word_count,
                      total: chapter.word_count,
                    })}
                  </div>
                  <Badge
                    variant={
                      chapter.status === "done" ? "success" : "secondary"
                    }
                  >
                    {projectStatusLabel(chapter.status, t)}
                  </Badge>
                </Link>
              ))}
            </div>
            {!chapters.data?.length && (
              <p className="text-sm text-muted-foreground">
                {project.isPending || chapters.isFetching
                  ? t("progress.loading")
                  : t("proofreading.chaptersAppearAfterPreparation")}
              </p>
            )}
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}
