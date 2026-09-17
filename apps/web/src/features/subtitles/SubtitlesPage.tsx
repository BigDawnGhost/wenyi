import { useI18n } from "@/i18n";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isProjectBusy } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { SegmentEditor } from "@/components/SegmentEditor";

export default function SubtitlesPage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const { data: project } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
  });
  const subtitles = useQuery({
    queryKey: ["subtitles", pid],
    queryFn: () => api.getSubtitles(pid),
    refetchInterval: isProjectBusy(project?.status) ? 3000 : false,
  });
  const busy = isProjectBusy(project?.status);
  const cues =
    subtitles.data?.cues.filter((c) =>
      `${c.source}\n${c.target || ""}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    ) || [];
  const pageCount = Math.max(1, Math.ceil(cues.length / 50));
  const current = Math.min(page, pageCount - 1);
  return (
    <>
      <PageHeader
        title={tr("common.subtitleEditor")}
        subtitle={tr("subtitles.translatedCuesOriginalIdsAndTimestampsAre", {
          completed: subtitles.data?.completed || 0,
          total: subtitles.data?.total || 0,
        })}
        actions={
          <Link to={`/projects/${pid}/export`}>
            <Button variant="outline">{tr("subtitles.exportSrt")}</Button>
          </Link>
        }
      />
      <PageContainer className="space-y-4">
        <ErrorNotice error={subtitles.error} />
        {busy && (
          <p className="rounded border p-3 text-sm">
            {tr("subtitles.aSubtitleTaskIsRunningContentRefreshes")}
          </p>
        )}
        <Input
          aria-label={tr("subtitles.searchSubtitles")}
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
          placeholder={tr("subtitles.searchSourceOrTranslation")}
        />
        <Card>
          <CardContent className="p-0">
            {cues.slice(current * 50, (current + 1) * 50).map((cue) => (
              <section key={cue.id} className="border-b last:border-0">
                <header className="px-3 py-2 bg-muted/40 text-xs text-muted-foreground">
                  #{cue.id} · {cue.start} → {cue.end}
                </header>
                <div className="grid md:grid-cols-2">
                  <div className="p-3 text-sm whitespace-pre-wrap border-r">
                    {cue.source}
                  </div>
                  <SegmentEditor
                    value={cue.target || ""}
                    disabled={busy}
                    onSave={async (target) => {
                      await api.editSubtitle(pid, cue.id, target);
                      await qc.invalidateQueries({
                        queryKey: ["subtitles", pid],
                      });
                    }}
                  />
                </div>
              </section>
            ))}
            {!cues.length && (
              <p className="p-8 text-sm text-center text-muted-foreground">
                {tr("subtitles.noMatchingSubtitles")}
              </p>
            )}
          </CardContent>
        </Card>
        <div className="flex justify-between items-center text-sm">
          <Button
            variant="outline"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
          >
            {tr("subtitles.previousPage")}
          </Button>
          <span>
            {tr("subtitles.pagination", {
              page: current + 1,
              pages: pageCount,
              count: cues.length,
            })}
          </span>
          <Button
            variant="outline"
            disabled={current + 1 >= pageCount}
            onClick={() => setPage(current + 1)}
          >
            {tr("subtitles.nextPage")}
          </Button>
        </div>
      </PageContainer>
    </>
  );
}
