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
import { BookOpen, Download, LoaderCircle, Pause, Play, RefreshCw, ShieldCheck } from "lucide-react";

export default function ProgressPage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { data: project } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    enabled: !!pid,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "translating" || status === "preparing" || status === "reviewing" || status === "qa"
        ? 3000
        : false;
    },
  });
  const { data: chapters, refetch } = useQuery({ queryKey: ["chapters", pid], queryFn: () => api.listChapters(pid), enabled: !!pid, refetchInterval: 3000 });
  const { data: qa } = useQuery({
    queryKey: ["qa", pid],
    queryFn: () => api.getQA(pid),
    enabled: !!pid,
    refetchInterval: (query) => query.state.data?.status === "running" ? 2000 : false,
  });
  const { msg, log, connected } = useProjectProgress(pid);

  // 实时进度优先用 WS 数据
  const done = chapters?.filter((c) => c.status === "done").length ?? 0;
  const total = chapters?.length ?? 0;
  const hasTranslationProgress = msg?.kind !== "qa"
    && typeof msg?.done === "number"
    && typeof msg?.total === "number"
    && msg.total > 0;
  const wsDone = hasTranslationProgress ? msg.done! : done;
  const wsTotal = hasTranslationProgress ? msg.total! : total;
  const pct = wsTotal ? Math.round((wsDone / wsTotal) * 100) : 0;

  const pause = useMutation({ mutationFn: () => api.pause(pid), onSuccess: () => { qc.invalidateQueries({ queryKey: ["project", pid] }); toast.success("已暂停"); } });
  const resume = useMutation({ mutationFn: () => api.resume(pid), onSuccess: () => { qc.invalidateQueries({ queryKey: ["project", pid] }); toast.success("已恢复"); } });
  const prepare = useMutation({ mutationFn: () => api.prepare(pid), onSuccess: () => { qc.invalidateQueries({ queryKey: ["project", pid] }); toast.success("已开始译前准备"); } });
  const runQA = useMutation({
    mutationFn: () => api.runQA(pid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", pid] });
      qc.invalidateQueries({ queryKey: ["qa", pid] });
      toast.success("已开始一致性检查");
    },
    onError: (error) => toast.error(`无法开始一致性检查：${error.message}`),
  });
  const regenerateReport = useMutation({
    mutationFn: () => api.regenerateReport(pid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["qa", pid] });
      toast.success("报告已重新生成");
    },
    onError: (error) => toast.error(`报告生成失败：${error.message}`),
  });

  const translating = project?.status === "translating";
  const preparing = project?.status === "preparing";
  const reviewing = project?.status === "reviewing";
  const qaRunning = project?.status === "qa" || qa?.status === "running";
  const paused = project?.status === "paused";
  const pipelineBusy = translating || preparing || reviewing;
  const busy = pipelineBusy || qaRunning;
  const translated = total > 0 && done === total;
  const currentStatus = paused
    ? "已暂停"
    : qaRunning
      ? (msg?.kind === "qa" ? msg.label : undefined) || "一致性检查中"
    : preparing
      ? msg?.label || "准备中"
      : reviewing
        ? msg?.label || "审校中"
        : translating
          ? msg?.label || "翻译中"
          : project?.status === "error"
            ? "失败"
            : project?.status || "—";

  return (
    <>
      <PageHeader
        title={project?.name || "翻译进度"}
        subtitle={project?.title || undefined}
        actions={
          <>
            {!busy && <Button variant="outline" onClick={() => prepare.mutate()} disabled={prepare.isPending}><BookOpen className="h-4 w-4" /> 译前准备</Button>}
            {pipelineBusy && <Button variant="outline" onClick={() => pause.mutate()}><Pause className="h-4 w-4" /> 暂停</Button>}
            {paused && <Button onClick={() => resume.mutate()}><Play className="h-4 w-4" /> 恢复</Button>}
            <Button
              variant="outline"
              onClick={() => runQA.mutate()}
              disabled={!translated || busy || runQA.isPending}
              title={translated ? "扫描跨章一致性问题" : "翻译全部完成后可用"}
            >
              {qaRunning || runQA.isPending
                ? <LoaderCircle className="h-4 w-4 animate-spin" />
                : <ShieldCheck className="h-4 w-4" />}
              一致性检查
            </Button>
            <Button
              variant="outline"
              onClick={() => regenerateReport.mutate()}
              disabled={!translated || busy || regenerateReport.isPending}
              title={translated ? "根据当前数据重生成报告（不调用模型）" : "翻译全部完成后可用"}
            >
              <RefreshCw className={`h-4 w-4 ${regenerateReport.isPending ? "animate-spin" : ""}`} />
              重生成报告
            </Button>
            <Link to={`/projects/${pid}/export`}><Button variant="outline"><Download className="h-4 w-4" /> 导出</Button></Link>
          </>
        }
      />
      <PageContainer className="space-y-4">
        <div className="grid gap-3 md:grid-cols-4">
          <StatCard label="翻译进度" value={`${wsDone}/${wsTotal}`} sub={`${pct}%`}><Progress value={pct} className="mt-2" /></StatCard>
          <StatCard label="当前状态" value={currentStatus} sub={connected ? "实时连接" : "离线"} />
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

        <ConsistencyResults
          status={qa?.status || "idle"}
          issues={qa?.issues || []}
          error={qa?.error}
          translated={translated}
          reportSummary={regenerateReport.data?.summary}
        />

        <ChapterTable pid={pid} chapters={chapters || []} />
      </PageContainer>
    </>
  );
}

const issueTypeLabels: Record<string, string> = {
  terminology: "术语漂移",
  pronoun: "代词 / 性别",
  tone: "语气偏移",
  punctuation: "标点",
};

function formatIssueLocation(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join("、");
  if (typeof value === "string" || typeof value === "number") return String(value);
  return "未指定章节";
}

function ConsistencyResults({
  status,
  issues,
  error,
  translated,
  reportSummary,
}: {
  status: import("@/lib/api").QAResult["status"];
  issues: import("@/lib/api").ConsistencyIssue[];
  error?: string | null;
  translated: boolean;
  reportSummary?: import("@/lib/api").ReportSummary;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="font-medium">跨章一致性</div>
            <div className="mt-0.5 text-xs text-muted-foreground">术语、代词与性别、语气和标点扫描结果</div>
          </div>
          {status === "running"
            ? <Badge variant="info">检查中</Badge>
            : status === "completed"
              ? <Badge variant={issues.length > 0 ? "warning" : "success"}>{issues.length > 0 ? `${issues.length} 项问题` : "未发现问题"}</Badge>
              : status === "error"
                ? <Badge variant="destructive">检查失败</Badge>
                : <Badge variant="secondary">尚未检查</Badge>}
        </div>

        {status === "running" && (
          <div className="mt-4 flex items-center gap-2 rounded-md bg-muted p-3 text-sm text-muted-foreground">
            <LoaderCircle className="h-4 w-4 animate-spin" />
            正在扫描各章译文，完成后结果会自动刷新。
          </div>
        )}
        {status === "idle" && (
          <div className="mt-4 text-sm text-muted-foreground">
            {translated ? "点击“一致性检查”开始扫描。" : "全部章节翻译完成后即可运行一致性检查。"}
          </div>
        )}
        {status === "error" && (
          <div className="mt-4 text-sm text-destructive">
            检查未完成{error ? `：${error}` : "，请重试或查看实时日志。"}
          </div>
        )}
        {status === "completed" && issues.length === 0 && (
          <div className="mt-4 text-sm text-muted-foreground">当前译文未发现跨章一致性问题。</div>
        )}
        {issues.length > 0 && (
          <div className="mt-4 divide-y rounded-md border">
            {issues.map((issue, index) => (
              <div key={`${issue.type}-${index}`} className="p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">{issueTypeLabels[issue.type] || issue.type || "其他"}</Badge>
                  <span className="text-xs text-muted-foreground">{formatIssueLocation(issue.where)}</span>
                </div>
                <p className="mt-2 text-sm">{issue.detail || "未提供详细说明"}</p>
              </div>
            ))}
          </div>
        )}
        {reportSummary && (
          <div className="mt-4 border-t pt-4">
            <div className="text-xs font-medium text-muted-foreground">最新报告摘要</div>
            <div className="mt-2 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
              <div>完成章节 {reportSummary.chapters_done}/{reportSummary.chapters_total}</div>
              <div>术语 {reportSummary.terms}</div>
              <div>待裁决冲突 {reportSummary.open_conflicts}</div>
              <div>审校问题 {reportSummary.review_issues}</div>
              <div>已审章节 {reportSummary.chapters_reviewed}</div>
              <div>回译疑点 {reportSummary.backtranslation_issues}</div>
              <div>空译文 {reportSummary.empty_targets}</div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
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
