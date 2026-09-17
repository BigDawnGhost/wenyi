import { useParams, Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  isProjectBusy,
  STATUS_LABELS,
  type ChapterSummary,
} from "@/lib/api";
import { useProjectProgress } from "@/lib/ws";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { ErrorNotice, StructuredData } from "@/components/ui/data";
import { WorkflowPanel } from "./WorkflowPanel";
import { toast } from "sonner";

export default function ProgressPage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const projectQuery = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 2500,
  });
  const project = projectQuery.data;
  const subtitle = project?.fmt === "srt";
  const chapterQuery = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
    enabled: !!project && !subtitle,
    refetchInterval: 3000,
  });
  const { data: cues } = useQuery({
    queryKey: ["subtitles", pid],
    queryFn: () => api.getSubtitles(pid),
    enabled: subtitle,
    refetchInterval: 3000,
  });
  const report = useQuery({
    queryKey: ["report", pid],
    queryFn: () => api.getReport(pid),
    enabled: !!project && !subtitle,
  });
  const stats = useQuery({
    queryKey: ["stats", pid],
    queryFn: () => api.getStats(pid),
    enabled: !!project,
    refetchInterval: isProjectBusy(project?.status) ? 5000 : false,
  });
  const { msg, connected } = useProjectProgress(pid);
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["project", pid] });
    qc.invalidateQueries({ queryKey: ["chapters", pid] });
    qc.invalidateQueries({ queryKey: ["stats", pid] });
  };
  const action = useMutation({
    mutationFn: async (
      kind: "pause" | "resume" | "prepare" | "translate" | "assemble",
    ) => api[kind](pid),
    onSuccess: (_, kind) => {
      invalidate();
      toast.success(
        kind === "pause" ? "已请求暂停，正在保存已完成结果" : "任务已提交",
      );
    },
  });
  const regenerate = useMutation({
    mutationFn: () => api.regenerateReport(pid),
    onSuccess: (result) => {
      qc.setQueryData(["report", pid], result);
      toast.success("报告已更新");
    },
  });
  const chapters = chapterQuery.data || [];
  // Match CLI translation progress: segment counts, not finished chapters.
  const done = subtitle
    ? cues?.completed || 0
    : chapters.reduce((sum, c) => sum + (c.target_word_count || 0), 0);
  const total = subtitle
    ? cues?.total || 0
    : chapters.reduce((sum, c) => sum + (c.word_count || 0), 0) ||
      project?.total_word_count ||
      0;
  const busy = isProjectBusy(project?.status);
  const paused = project?.status === "paused";
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <>
      <PageHeader
        title={project?.name || "翻译进度"}
        subtitle={`${project?.source_lang || "自动检测"} → ${project?.target_lang || "—"} · ${project?.title || "等待原文"}`}
        actions={
          <>
            {!busy && !paused && project?.fmt && (
              <Button
                disabled={action.isPending}
                onClick={() => action.mutate("translate")}
              >
                开始翻译
              </Button>
            )}
            {busy && project?.status !== "parsing" && (
              <Button
                variant="outline"
                disabled={action.isPending || project?.status === "pausing"}
                onClick={() => action.mutate("pause")}
              >
                暂停
              </Button>
            )}
            {(paused || project?.status === "error") && (
              <Button
                disabled={action.isPending}
                onClick={() => action.mutate("resume")}
              >
                恢复任务
              </Button>
            )}
            <Link to={`/projects/${pid}/export`}>
              <Button variant="outline">导出</Button>
            </Link>
          </>
        }
      />
      <PageContainer className="space-y-4">
        <ErrorNotice
          error={
            projectQuery.error ||
            chapterQuery.error ||
            action.error ||
            project?.error
          }
        />
        <WorkflowPanel pid={pid} msg={msg} />
        <div className="grid gap-3 md:grid-cols-4">
          <Stat
            label={subtitle ? "字幕翻译进度" : "翻译进度"}
            value={`${done}/${total}`}
          >
            <Progress value={pct} className="mt-2" />
          </Stat>
          <Stat
            label="当前状态"
            value={
              STATUS_LABELS[project?.status || ""] ||
              project?.status ||
              "加载中"
            }
          />
          <Stat
            label="当前步骤"
            value={busy ? msg?.label || "等待后台进度" : "—"}
          />
          <Stat label="进度连接" value={connected ? "实时连接" : "自动轮询"} />
        </div>
        <Card>
          <CardContent className="p-4 space-y-3">
            <div className="flex flex-wrap gap-2">
              {!subtitle && (
                <Button
                  variant="outline"
                  disabled={busy || !project?.fmt || action.isPending}
                  onClick={() => action.mutate("prepare")}
                >
                  译前准备
                </Button>
              )}
              {!subtitle && (
                <Link to={`/projects/${pid}/review`}>
                  <Button variant="outline">全书审校</Button>
                </Link>
              )}
              {subtitle && (
                <Link to={`/projects/${pid}/subtitles`}>
                  <Button variant="outline">字幕对照与编辑</Button>
                </Link>
              )}
              <Link to={`/projects/${pid}/settings`}>
                <Button variant="outline">项目配置与模型</Button>
              </Link>
              {!project?.initialized && (
                <Link to={`/projects/new?project=${pid}`}>
                  <Button variant="outline">上传与预览原文</Button>
                </Link>
              )}
              <Button
                variant="outline"
                disabled={done === 0 || action.isPending}
                onClick={() => action.mutate("assemble")}
              >
                重新组装默认格式
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              导出使用已保存译文的一致快照。暂停会在安全边界保存进度，恢复继续原来的任务。
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 space-y-3">
            <h2 className="font-medium">累计用量与运行耗时</h2>
            <ErrorNotice error={stats.error} />
            <Accounting value={stats.data} />
          </CardContent>
        </Card>
        {!subtitle && (
          <>
            <Card>
              <CardContent className="p-4 space-y-3">
                <div className="flex justify-between items-center">
                  <h2 className="font-medium">项目报告</h2>
                  <Button
                    variant="outline"
                    disabled={regenerate.isPending || busy}
                    onClick={() => regenerate.mutate()}
                  >
                    更新报告
                  </Button>
                </div>
                <ErrorNotice error={report.error || regenerate.error} />
                <StructuredData value={report.data?.summary} />
              </CardContent>
            </Card>
            <ChapterTable pid={pid} chapters={chapters} busy={busy} />
          </>
        )}
      </PageContainer>
    </>
  );
}

function Stat({
  label,
  value,
  children,
}: {
  label: string;
  value: string;
  children?: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="font-semibold mt-2 break-words">{value}</div>
        {children}
      </CardContent>
    </Card>
  );
}

function ChapterTable({
  pid,
  chapters,
  busy,
}: {
  pid: string;
  chapters: ChapterSummary[];
  busy: boolean;
}) {
  const qc = useQueryClient();
  const translate = useMutation({
    mutationFn: (ci: number) => api.translateChapter(pid, ci),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", pid] });
      qc.invalidateQueries({ queryKey: ["chapters", pid] });
      toast.success("单章翻译已开始");
    },
  });
  return (
    <Card>
      <CardContent className="p-0 overflow-x-auto">
        <ErrorNotice error={translate.error} />
        <table className="w-full text-sm">
          <thead className="border-b text-xs text-muted-foreground">
            <tr>
              {["章节", "原文段数", "翻译状态", "审校状态", "操作"].map((h) => (
                <th key={h} className="text-left p-3 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {chapters.map((c) => (
              <tr key={c.index} className="border-b last:border-0">
                <td className="p-3">
                  {c.index + 1}. {c.title_translated || c.title}
                </td>
                <td className="p-3">{c.word_count}</td>
                <td className="p-3">
                  <Badge
                    variant={c.status === "done" ? "success" : "secondary"}
                  >
                    {STATUS_LABELS[c.status] || c.status}
                  </Badge>
                </td>
                <td className="p-3">
                  {c.review_issue_count > 0 ? (
                    <Badge variant="warning">
                      {c.review_issue_count} 项意见
                    </Badge>
                  ) : ["completed", "ok", "done"].includes(
                      c.review_status || "",
                    ) ? (
                    <Badge variant="success">已审校</Badge>
                  ) : (
                    <Badge variant="secondary">未审校</Badge>
                  )}
                </td>
                <td className="p-3">
                  {c.status === "done" ? (
                    <Link
                      className="text-primary underline"
                      to={`/projects/${pid}/review/${c.index}`}
                    >
                      人工校阅
                    </Link>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy || translate.isPending}
                      onClick={() => translate.mutate(c.index)}
                    >
                      翻译此章
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {!chapters.length && (
              <tr>
                <td
                  colSpan={5}
                  className="p-8 text-center text-muted-foreground"
                >
                  解析完成后显示章节。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function Accounting({
  value,
}: {
  value?: { usage: Record<string, unknown>; timing: Record<string, unknown> };
}) {
  if (!value)
    return <p className="text-sm text-muted-foreground">尚无用量记录</p>;
  const usage = value.usage || {};
  const totals = (usage.totals || usage) as Record<string, unknown>;
  const timing = value.timing || {};
  const numeric = (value: unknown) =>
    typeof value === "number" ? value.toLocaleString("zh-CN") : "—";
  const fields: [string, string][] = [
    ["累计 Token", numeric(totals.total_tokens)],
    [
      "输入 / 输出 Token",
      `${numeric(totals.prompt_tokens)} / ${numeric(totals.completion_tokens)}`,
    ],
    ["模型调用次数", numeric(totals.calls)],
    [
      "运行耗时",
      typeof timing.total_seconds === "number"
        ? `${timing.total_seconds.toFixed(2)} 秒`
        : "—",
    ],
  ];
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {fields.map(([label, count]) => (
          <div key={label} className="rounded border p-3">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="font-semibold mt-2">{count}</div>
          </div>
        ))}
      </div>
      <details>
        <summary className="cursor-pointer text-sm text-muted-foreground">
          按模型、提供商与步骤查看用量和计时明细
        </summary>
        <div className="mt-3">
          <StructuredData value={value} />
        </div>
      </details>
    </div>
  );
}
