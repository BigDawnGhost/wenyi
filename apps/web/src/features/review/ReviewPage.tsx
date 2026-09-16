import { useEffect, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, isProjectBusy, type ReviewRun } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/form";
import { ErrorNotice, StructuredData } from "@/components/ui/data";

export default function ReviewPage() {
  const { pid = "", ci } = useParams();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string>();
  const [autofix, setAutofix] = useState<boolean>();
  const project = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
  });
  const config = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const subtitle = project.data?.fmt === "srt";
  const chapters = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
    enabled: !subtitle,
    refetchInterval: 4000,
  });
  const runs = useQuery({
    queryKey: ["review-runs", pid],
    queryFn: () => api.listReviewRuns(pid),
    enabled: !subtitle,
    refetchInterval: 3000,
  });
  const rid = selected || runs.data?.[0]?.id;
  const run = useQuery({
    queryKey: ["review-run", pid, rid],
    queryFn: () => api.getReviewRun(pid, rid!),
    enabled: !!rid && !subtitle,
    refetchInterval: isProjectBusy(project.data?.status) ? 3000 : false,
  });
  const busy = isProjectBusy(project.data?.status);
  const configuredAutofix = Boolean(
    (config.data?.effective.pipeline as Record<string, unknown> | undefined)
      ?.review_autofix ?? true,
  );
  const allowAutofix = autofix ?? configuredAutofix;
  const review = useMutation({
    mutationFn: () => api.runAiReview(pid, { autofix: allowAutofix }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", pid] });
      qc.invalidateQueries({ queryKey: ["review-runs", pid] });
      toast.success("全书审校已提交");
    },
  });
  const translated =
    !!chapters.data?.length && chapters.data.every((c) => c.status === "done");
  if (subtitle) return <Navigate to={`/projects/${pid}/subtitles`} replace />;
  if (ci !== undefined)
    return <ManualReview pid={pid} index={Number(ci)} busy={busy} />;
  return (
    <>
      <PageHeader
        title="全书审校"
        subtitle="查看审校问题、核查证据、建议修订与自动修复发布结果"
      />
      <PageContainer className="space-y-4">
        <ErrorNotice
          error={
            project.error ||
            config.error ||
            chapters.error ||
            runs.error ||
            run.error ||
            review.error
          }
        />
        <Card>
          <CardContent className="p-5 space-y-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={allowAutofix}
                disabled={busy || review.isPending}
                onChange={(e) => setAutofix(e.target.checked)}
              />
              审校后自动修复并写回正式译文
            </label>
            <p className="text-sm text-muted-foreground">
              内容、配置和术语未变化时复用已完成结果；符合恢复条件的中断审校会继续原有运行。关闭自动修复时保留正式译文。
            </p>
            <Button
              disabled={!translated || busy || review.isPending}
              onClick={() => review.mutate()}
            >
              {review.isPending ? "提交中…" : "运行全书审校"}
            </Button>
            {!translated && (
              <p className="text-sm text-muted-foreground">
                所有章节翻译完成后可运行全书审校。
              </p>
            )}
            {busy && (
              <p role="status" className="text-sm text-muted-foreground">
                项目正在执行任务，审校结果会自动刷新。可在进度页暂停并恢复。
              </p>
            )}
          </CardContent>
        </Card>
        <div className="grid lg:grid-cols-[280px_1fr] gap-4">
          <Card>
            <CardContent className="p-4 space-y-3">
              <h2 className="font-medium">审校运行记录</h2>
              {!runs.data?.length && (
                <p className="text-sm text-muted-foreground">尚未审校</p>
              )}
              {runs.data?.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setSelected(r.id)}
                  className={`w-full text-left rounded border p-3 text-sm ${rid === r.id ? "border-primary bg-accent" : "hover:bg-muted"}`}
                >
                  <div className="break-all">
                    {r.created_at
                      ? new Date(r.created_at).toLocaleString()
                      : r.id}
                  </div>
                  <Badge variant="secondary" className="mt-2">
                    {reviewStatus(r.status)}
                  </Badge>
                </button>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-5 space-y-5">
              {run.data ? (
                <RunDetail run={run.data} />
              ) : (
                <p className="text-sm text-muted-foreground">
                  选择运行查看结果。未执行审校不表示没有问题。
                </p>
              )}
            </CardContent>
          </Card>
        </div>
        <Card>
          <CardContent className="p-4">
            <h2 className="font-medium mb-3">人工逐章校阅</h2>
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {chapters.data
                ?.filter((c) => c.status === "done")
                .map((c) => (
                  <Link
                    key={c.index}
                    className="rounded border p-3 text-sm hover:bg-accent"
                    to={`/projects/${pid}/review/${c.index}`}
                  >
                    {c.index + 1}. {c.title_translated || c.title}
                  </Link>
                ))}
            </div>
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}

function RunDetail({ run }: { run: ReviewRun }) {
  return (
    <>
      <div className="flex justify-between gap-3">
        <h2 className="font-medium break-all">审校 {run.id}</h2>
        <Badge variant="secondary">{reviewStatus(run.status)}</Badge>
      </div>
      <section>
        <h3 className="font-medium mb-3">运行摘要</h3>
        <ReviewSummary summary={run.summary} />
      </section>
      <section>
        <h3 className="font-medium mb-3">问题与证据</h3>
        <StructuredData
          value={run.issues}
          empty={
            run.status === "completed"
              ? "本次审校没有已记录的问题"
              : "暂无问题记录；运行可能尚未完成"
          }
        />
      </section>
      <section>
        <h3 className="font-medium mb-3">建议变更</h3>
        <StructuredData value={run.changes} />
      </section>
      <section>
        <h3 className="font-medium mb-3">自动修复与发布记录</h3>
        <StructuredData
          value={Object.fromEntries(
            Object.entries(run.autofix || {}).filter(
              ([key]) => key !== "index",
            ),
          )}
          empty="尚无发布记录"
        />
      </section>
      <details>
        <summary className="cursor-pointer text-sm text-muted-foreground">
          完整运行与检查点信息
        </summary>
        <pre className="mt-3 text-xs whitespace-pre-wrap break-all overflow-auto max-h-96">
          {JSON.stringify(run, null, 2)}
        </pre>
      </details>
    </>
  );
}

function ManualReview({
  pid,
  index,
  busy,
}: {
  pid: string;
  index: number;
  busy: boolean;
}) {
  const qc = useQueryClient();
  const chapter = useQuery({
    queryKey: ["review", pid, index],
    queryFn: () => api.getReview(pid, index),
    enabled: Number.isInteger(index) && index >= 0,
  });
  const chapters = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
  });
  const reviewable = chapters.data?.filter((c) => c.status === "done") || [];
  const current = reviewable.findIndex((c) => c.index === index);
  const previous = reviewable[current - 1];
  const next = reviewable[current + 1];
  const complete = useMutation({
    mutationFn: () => api.markReviewComplete(pid, index),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["chapters", pid] });
      toast.success("已标记人工校阅完成");
    },
  });
  return (
    <>
      <PageHeader
        title={`人工校阅 — ${chapter.data?.title_translated || chapter.data?.title || "加载中"}`}
        subtitle="按段对照原文与译文，保存成功后更新正式译文"
        actions={
          <>
            <Link to={`/projects/${pid}/review`}>
              <Button variant="outline">全书审校</Button>
            </Link>
            {previous && (
              <Link to={`/projects/${pid}/review/${previous.index}`}>
                <Button variant="outline">上一章</Button>
              </Link>
            )}
            {next && (
              <Link to={`/projects/${pid}/review/${next.index}`}>
                <Button variant="outline">下一章</Button>
              </Link>
            )}
            <Button
              disabled={busy || !chapter.data || complete.isPending}
              onClick={() => complete.mutate()}
            >
              标记人工校阅完成
            </Button>
          </>
        }
      />
      <PageContainer className="space-y-4">
        <ErrorNotice error={chapter.error || complete.error} />
        {busy && (
          <p className="rounded border p-3 text-sm">
            项目任务执行中，人工修改暂时只读。
          </p>
        )}
        <Card>
          <CardContent className="p-0">
            <div className="grid grid-cols-2 border-b p-3 text-sm font-medium">
              <span>原文</span>
              <span>译文</span>
            </div>
            {chapter.data?.segments
              .filter((s) => s.source?.trim())
              .map((s) => (
                <div
                  key={`${index}-${s.index}`}
                  className="grid md:grid-cols-2 border-b last:border-0"
                >
                  <div className="p-3 whitespace-pre-wrap text-sm border-r">
                    <span className="text-xs text-muted-foreground mr-2">
                      #{s.index + 1}
                    </span>
                    {s.source}
                  </div>
                  <div>
                    <SegmentEditor
                      value={s.target || ""}
                      disabled={busy}
                      onSave={async (target) => {
                        await api.editSegment(pid, index, s.index, target);
                        await qc.invalidateQueries({
                          queryKey: ["review", pid, index],
                        });
                      }}
                    />
                    {s.target_before_polish && (
                      <details className="px-3 pb-3 text-sm">
                        <summary className="text-muted-foreground cursor-pointer">
                          润色前译文
                        </summary>
                        <p className="mt-2 whitespace-pre-wrap">
                          {s.target_before_polish}
                        </p>
                      </details>
                    )}
                  </div>
                </div>
              ))}
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 space-y-3">
            <h2 className="font-medium">本章已记录的审校意见</h2>
            <StructuredData
              value={chapter.data?.review_issues}
              empty="暂无已记录的意见。请在全书审校页查看运行状态。"
            />
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}

export function SegmentEditor({
  value,
  disabled,
  onSave,
}: {
  value: string;
  disabled: boolean;
  onSave: (target: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const save = useMutation({
    mutationFn: () => onSave(draft),
    onSuccess: () => {
      setEditing(false);
      toast.success("译文已保存");
    },
  });
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);
  return (
    <div className="p-3 text-sm">
      <ErrorNotice error={save.error} />
      {editing ? (
        <div className="space-y-2">
          <Textarea
            aria-label="编辑译文"
            value={draft}
            disabled={disabled || save.isPending}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              disabled={disabled || save.isPending}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "保存中…" : "保存译文"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={save.isPending}
              onClick={() => setEditing(false)}
            >
              取消
            </Button>
          </div>
        </div>
      ) : (
        <button
          disabled={disabled}
          className="text-left w-full whitespace-pre-wrap min-h-10 rounded hover:bg-muted disabled:cursor-default"
          onClick={() => {
            setDraft(value);
            save.reset();
            setEditing(true);
          }}
        >
          {value || (
            <span className="text-muted-foreground">（空译文，点击编辑）</span>
          )}
        </button>
      )}
    </div>
  );
}

function reviewStatus(status: string) {
  return (
    (
      {
        completed: "已完成",
        running: "审校中",
        interrupted: "已中断",
        error: "失败",
        failed: "失败",
        pending: "等待执行",
      } as Record<string, string>
    )[status] || status
  );
}
function ReviewSummary({ summary }: { summary: Record<string, unknown> }) {
  const keys: [string, string][] = [
    ["issue_count", "审校问题"],
    ["change_count", "建议变更"],
    ["conflict_count", "冲突"],
    ["review_round_count", "审校轮次"],
    ["autofix_applied_segment_count", "已修复段落"],
    ["autofix_failed_issue_count", "修复失败"],
  ];
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3">
        {keys.map(([key, label]) => (
          <div key={key} className="rounded border p-3">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="font-semibold mt-1">
              {typeof summary[key] === "number" ? String(summary[key]) : "—"}
            </div>
          </div>
        ))}
      </div>
      <details>
        <summary className="cursor-pointer text-xs text-muted-foreground">
          查看全部运行计数
        </summary>
        <div className="mt-3">
          <StructuredData value={summary} />
        </div>
      </details>
    </div>
  );
}
