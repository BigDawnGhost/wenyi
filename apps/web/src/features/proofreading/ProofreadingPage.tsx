import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useI18n } from "@/i18n";
import { statusLabel } from "@/i18n/status";
import { api, isProjectBusy } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/StatusBadge";
import { Input, Label, Select } from "@/components/ui/form";
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
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const filtered = chapters.data?.filter(
    (chapter) =>
      (!status || chapter.status === status) &&
      `${chapter.title} ${chapter.title_translated || ""}`
        .toLocaleLowerCase()
        .includes(search.trim().toLocaleLowerCase()),
  );
  const statuses = [
    ...new Set(chapters.data?.map((chapter) => chapter.status)),
  ];
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
            <div className="flex flex-wrap gap-3">
              <div className="flex-1 min-w-48 space-y-1">
                <Label htmlFor="chapter-search">
                  {t("proofreading.search")}
                </Label>
                <Input
                  id="chapter-search"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="chapter-status">
                  {t("proofreading.filter")}
                </Label>
                <Select
                  id="chapter-status"
                  value={status}
                  onChange={(event) => setStatus(event.target.value)}
                >
                  <option value="">{t("list.all")}</option>
                  {statuses.map((code) => (
                    <option key={code} value={code}>
                      {statusLabel(code, t, "chapter")}
                    </option>
                  ))}
                </Select>
              </div>
            </div>
            <ul aria-label={t("proofreading.chapterList")} className="divide-y">
              {filtered?.map((chapter) => (
                <li key={chapter.index}>
                  <Link
                    className="flex items-center justify-between gap-4 py-4 hover:bg-accent rounded"
                    to={`/projects/${pid}/proofreading/${chapter.index}`}
                  >
                    <div className="min-w-0 space-y-1">
                      <div className="text-sm font-medium [overflow-wrap:anywhere]">
                        {chapter.title_translated?.trim() ||
                          chapter.title.trim() ||
                          t("common.untitledChapter")}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {t("proofreading.savedParagraphs", {
                          done: chapter.target_word_count,
                          total: chapter.word_count,
                        })}
                      </div>
                    </div>
                    <span className="shrink-0">
                      <StatusBadge status={chapter.status} context="chapter" />
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
            {!!chapters.data?.length && !filtered?.length && (
              <p className="text-sm text-muted-foreground">
                {t("list.noMatches")}
              </p>
            )}
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
