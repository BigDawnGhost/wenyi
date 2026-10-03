import { useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useI18n } from "@/i18n";
import { api, isProjectBusy, type ChapterSummary } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { ErrorNotice } from "@/components/ui/data";
import { Input } from "@/components/ui/form";
import { ContentsEditor } from "./ContentsEditor";
import { ContentsList } from "./ContentsList";

export default function ContentsPage() {
  const { pid = "" } = useParams();
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
  if (subtitle) return <Navigate to={`/projects/${pid}/subtitles`} replace />;
  return (
    <ContentsWorkspace
      key={pid}
      pid={pid}
      chapters={chapters.data || []}
      loading={project.isPending || chapters.isPending}
      error={project.error || chapters.error}
      busy={isProjectBusy(project.data?.status)}
    />
  );
}

function ContentsWorkspace({
  pid,
  chapters,
  loading,
  error,
  busy,
}: {
  pid: string;
  chapters: ChapterSummary[];
  loading: boolean;
  error: unknown;
  busy: boolean;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const entries = chapters.map((chapter) => {
    const id = String(chapter.index);
    return {
      id,
      title: chapter.title,
      title_translated: chapter.title_translated ?? null,
      depth: 0,
      chapter_index: chapter.index,
      segment_index: null,
    };
  });
  const active = entries.find((entry) => entry.id === editing);
  const query = search.trim().toLocaleLowerCase();
  const visible = entries.filter((entry) =>
    `${entry.title} ${entry.title_translated || ""}`
      .toLocaleLowerCase()
      .includes(query),
  );
  const readOnly = busy || !!error || loading;
  return (
    <>
      <PageHeader title={t("contents.title")} subtitle={t("contents.help")} />
      <PageContainer className="space-y-4">
        <ErrorNotice error={error} />
        {busy && (
          <p role="status" className="text-sm text-muted-foreground">
            {t("contents.pauseToEdit")}
          </p>
        )}
        <Input
          aria-label={t("contents.search")}
          placeholder={t("contents.search")}
          className="max-w-md"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        {loading ? (
          <p className="text-sm text-muted-foreground">
            {t("progress.loading")}
          </p>
        ) : (
          <ContentsList
            pid={pid}
            entries={visible}
            readOnly={readOnly}
            onEdit={setEditing}
          />
        )}
        {active && (
          <ContentsEditor
            key={`${active.id}:${active.title}`}
            source={active.title}
            target={active.title_translated}
            readOnly={readOnly}
            onClose={() => setEditing(null)}
            onSave={async (target, expected) => {
              try {
                await api.updateChapterTitle(pid, active.chapter_index, {
                  title_translated: target,
                  expected_title_translated: expected,
                });
              } finally {
                await Promise.all([
                  qc.invalidateQueries({ queryKey: ["chapters", pid] }),
                  qc.invalidateQueries({ queryKey: ["review", pid] }),
                  qc.invalidateQueries({ queryKey: ["events", pid] }),
                ]);
              }
              toast.success(t("contents.saved"));
            }}
          />
        )}
      </PageContainer>
    </>
  );
}
