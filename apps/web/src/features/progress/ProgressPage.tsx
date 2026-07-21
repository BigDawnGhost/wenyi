import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useProjectProgress } from "@/lib/ws";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, Pause, Play } from "lucide-react";

export default function ProgressPage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data: project } = useQuery({ queryKey: ["project", pid], queryFn: () => api.getProject(pid), enabled: !!pid });
  const { data: chapters, refetch } = useQuery({ queryKey: ["chapters", pid], queryFn: () => api.listChapters(pid), enabled: !!pid, refetchInterval: 3000 });
  const { msg, log, connected } = useProjectProgress(pid);

  // 实时进度优先用 WS 数据
  const done = chapters?.filter((c) => c.status === "done").length ?? 0;
  const total = chapters?.length ?? 0;
  const wsDone = typeof msg?.done === "number" && typeof msg?.total === "number" && msg.total > 0 ? msg.done : done;
  const wsTotal = typeof msg?.total === "number" && msg.total > 0 ? msg.total : total;
  const pct = wsTotal ? Math.round((wsDone / wsTotal) * 100) : 0;

  const pause = useMutation({ mutationFn: () => api.pause(pid), onSuccess: () => { qc.invalidateQueries({ queryKey: ["project", pid] }); toast.success("已暂停"); } });
  const resume = useMutation({ mutationFn: () => api.resume(pid), onSuccess: () => { qc.invalidateQueries({ queryKey: ["project", pid] }); toast.success("已恢复"); } });

  const translating = project?.status === "translating";
  const paused = project?.status === "paused";

  return (
    <>
      <PageHeader
        title={project?.name || "翻译进度"}
        subtitle={project?.title || undefined}
        actions={
          <>
            {translating && <Button variant="outline" onClick={() => pause.mutate()}><Pause className="h-4 w-4" /> 暂停</Button>}
            {paused && <Button onClick={() => resume.mutate()}><Play className="h-4 w-4" /> 恢复</Button>}
            <Link to={`/projects/${pid}/export`}><Button variant="outline"><Download className="h-4 w-4" /> 导出</Button></Link>
          </>
        }
      />
      <PageContainer className="space-y-4">
        <div className="grid gap-3 md:grid-cols-4">
          <StatCard label="翻译进度" value={`${wsDone}/${wsTotal}`} sub={`${pct}%`}><Progress value={pct} className="mt-2" /></StatCard>
          <StatCard label="当前状态" value={paused ? "已暂停" : msg?.label || (translating ? "翻译中" : project?.status || "—")} sub={connected ? "实时连接" : "离线"} />
          <StatCard label="策略" value={(project?.strategy as { template?: string })?.template || "自定义"} />
          <StatCard label="状态" value={project?.status || "—"} />
        </div>

        <Card>
          <CardContent className="p-4">
            <div className="text-xs text-muted-foreground mb-2">实时日志</div>
            <div className="h-36 overflow-auto rounded bg-zinc-950 text-zinc-100 p-3 font-mono text-xs space-y-0.5">
              {log.length === 0 ? <div className="text-zinc-500">等待事件…</div> : log.map((l, i) => <div key={i}>● {l}</div>)}
            </div>
          </CardContent>
        </Card>

        <ChapterTable pid={pid} chapters={chapters || []} />
      </PageContainer>
    </>
  );
}

function StatCard({ label, value, sub, children }: { label: string; value: string; sub?: string; children?: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="text-xl font-semibold mt-1 truncate">{value}</div>
        {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
        {children}
      </CardContent>
    </Card>
  );
}

function ChapterTable({ pid, chapters }: { pid: string; chapters: import("@/lib/api").ChapterSummary[] }) {
  return (
    <Card>
      <CardContent className="p-0">
        <table className="w-full text-sm">
          <thead className="border-b text-xs text-muted-foreground">
            <tr>
              <th className="text-left p-3 font-medium">章节</th>
              <th className="text-right p-3 font-medium">原文段数</th>
              <th className="text-left p-3 font-medium">状态</th>
              <th className="text-right p-3 font-medium">译文段数</th>
              <th className="text-left p-3 font-medium">审校</th>
              <th className="text-right p-3 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {chapters.map((c) => (
              <tr key={c.index} className="border-b last:border-0 hover:bg-muted/40">
                <td className="p-3">
                  <span className="text-xs text-muted-foreground mr-2">Ch.{c.index + 1}</span>
                  {c.title_translated || c.title || `第 ${c.index + 1} 章`}
                </td>
                <td className="p-3 text-right text-muted-foreground">{c.word_count}</td>
                <td className="p-3">
                  {c.status === "done" ? <Badge variant="success">完成</Badge> : c.status === "translating" ? <Badge variant="info">翻译中</Badge> : <Badge variant="secondary">等待</Badge>}
                </td>
                <td className="p-3 text-right text-muted-foreground">{c.status === "done" ? c.target_word_count : "—"}</td>
                <td className="p-3">
                  {c.status === "done" && (c.review_issue_count > 0 ? <Badge variant="warning">待审 {c.review_issue_count}</Badge> : <Badge variant="success">通过</Badge>)}
                </td>
                <td className="p-3 text-right">
                  {c.status === "done" && (
                    <Link to={`/projects/${pid}/review/${c.index}`} className="text-xs text-primary hover:underline">
                      {c.review_issue_count > 0 ? "审校" : "查看"}
                    </Link>
                  )}
                </td>
              </tr>
            ))}
            {chapters.length === 0 && <tr><td colSpan={6} className="p-8 text-center text-muted-foreground text-sm">尚无章节。上传原文并启动翻译后这里会出现章节列表。</td></tr>}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
