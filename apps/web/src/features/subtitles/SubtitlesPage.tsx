import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isProjectBusy } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { SegmentEditor } from "@/features/review/ReviewPage";

export default function SubtitlesPage() {
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
        title="字幕对照与编辑"
        subtitle={`已翻译 ${subtitles.data?.completed || 0} / ${subtitles.data?.total || 0} 条；保留原始序号和时间轴`}
        actions={
          <Link to={`/projects/${pid}/export`}>
            <Button variant="outline">导出 SRT</Button>
          </Link>
        }
      />
      <PageContainer className="space-y-4">
        <ErrorNotice error={subtitles.error} />
        {busy && (
          <p className="rounded border p-3 text-sm">
            字幕任务执行中，内容自动刷新。暂停完成后可编辑译文。
          </p>
        )}
        <Input
          aria-label="搜索字幕"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
          placeholder="搜索原文或译文…"
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
                暂无匹配的字幕。
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
            上一页
          </Button>
          <span>
            {current + 1} / {pageCount} 页 · {cues.length} 条
          </span>
          <Button
            variant="outline"
            disabled={current + 1 >= pageCount}
            onClick={() => setPage(current + 1)}
          >
            下一页
          </Button>
        </div>
      </PageContainer>
    </>
  );
}
