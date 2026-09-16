import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenText, Library, RefreshCw, X } from "lucide-react";
import { toast } from "sonner";
import { api, STATUS_LABELS, type Retranslation } from "@/lib/api";
import { cn } from "@/lib/utils";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import GlossaryPage from "@/features/glossary/GlossaryPage";

const POLL_INTERVAL = 2000;
const pending = (status: string) => status === "queued" || status === "running";

export default function ReaderPage() {
  const { pid = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [follow, setFollow] = useState(false);
  const [glossaryOpen, setGlossaryOpen] = useState(false);
  const paragraphRefs = useRef(new Map<number, HTMLElement>());
  const project = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: POLL_INTERVAL,
  });
  const book = !!project.data?.initialized && project.data.fmt !== "srt";
  const chapters = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
    enabled: book,
    refetchInterval: POLL_INTERVAL,
  });
  const requestedIndex = Number(params.get("chapter") || 0);
  const index = chapters.data?.some((chapter) => chapter.index === requestedIndex)
    ? requestedIndex
    : chapters.data?.[0]?.index ?? 0;
  const chapter = useQuery({
    queryKey: ["reader", pid, index],
    queryFn: () => api.getChapter(pid, index),
    enabled: book && !!chapters.data?.length,
    refetchInterval: POLL_INTERVAL,
  });
  const operations = useQuery({
    queryKey: ["retranslations", pid],
    queryFn: () => api.listRetranslations(pid),
    enabled: book,
    refetchInterval: POLL_INTERVAL,
  });
  const segments = chapter.data?.segments || [];
  const paragraphs = segments.filter((segment) => segment.kind === "text" && segment.source.trim());
  const translated = paragraphs.filter((segment) => segment.target != null);
  const latestIndex = translated.at(-1)?.index;
  const activeSegments = new Set(
    (operations.data || [])
      .filter((operation) => operation.chapter_index === index && pending(operation.status))
      .flatMap((operation) => operation.segment_indices),
  );
  const selectable = translated.filter((segment) => !activeSegments.has(segment.index));
  const selectedIndices = selectable
    .filter((segment) => selected.has(segment.index))
    .map((segment) => segment.index);

  useEffect(() => {
    setSelected(new Set());
    setFollow(false);
  }, [pid, index]);

  // A stable index means ordinary polls never move the reader's scroll position.
  useEffect(() => {
    if (follow && latestIndex !== undefined) {
      paragraphRefs.current.get(latestIndex)?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [follow, latestIndex, index]);

  const retranslate = useMutation({
    mutationFn: ({ chapterIndex, indices }: { chapterIndex: number; indices: number[] }) =>
      api.retranslateSegments(pid, chapterIndex, indices),
    onSuccess: (result, variables) => {
      qc.setQueryData<Retranslation[]>(["retranslations", pid], (previous = []) => [
        result,
        ...previous.filter((operation) => operation.id !== result.id),
      ]);
      if (variables.chapterIndex === index) {
        setSelected((previous) => new Set([...previous].filter((value) => !variables.indices.includes(value))));
      }
      qc.invalidateQueries({ queryKey: ["reader", pid, variables.chapterIndex] });
      toast.success("段落重译已提交，可继续阅读");
    },
  });

  const toggleSelection = (segmentIndex: number) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(segmentIndex)) next.delete(segmentIndex);
      else next.add(segmentIndex);
      return next;
    });
  };

  if (project.data?.fmt === "srt") {
    return <>
      <PageHeader title="边翻边看" subtitle="字幕请使用时间轴对照页面查看实时译文。" />
      <PageContainer>
        <Link className="text-primary underline" to={`/projects/${pid}/subtitles`}>打开字幕对照与编辑</Link>
      </PageContainer>
    </>;
  }

  return (
    <>
      <PageHeader
        title="边翻边看"
        subtitle={`${project.data?.name || "加载项目…"} · 已保存的译文每 2 秒自动更新`}
        actions={<>
          <Badge variant="secondary">{STATUS_LABELS[project.data?.status || ""] || project.data?.status || "加载中"}</Badge>
          <Link to={`/projects/${pid}`}><Button variant="outline">翻译进度</Button></Link>
        </>}
      />
      <div className="sticky top-0 z-20 border-b bg-background/95 px-4 py-3 sm:px-6 backdrop-blur space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <Select
            aria-label="阅读章节"
            className="max-w-md"
            value={String(index)}
            disabled={!chapters.data?.length || retranslate.isPending}
            onChange={(event) => {
              setParams({ chapter: event.target.value });
              retranslate.reset();
            }}
          >
            {!chapters.data?.length && <option value="0">等待章节初始化</option>}
            {chapters.data?.map((item) => <option key={item.index} value={item.index}>
              {item.index + 1}. {item.title_translated || item.title || "无标题"} · {STATUS_LABELS[item.status] || item.status}
            </option>)}
          </Select>
          <span className="text-sm text-muted-foreground" aria-live="polite">已译 {translated.length} / {paragraphs.length} 段</span>
          <Button variant="outline" onClick={() => setGlossaryOpen(true)} disabled={!book}>
            <Library className="h-4 w-4" /> 编辑术语表
          </Button>
          <Button variant="ghost" aria-label="立即刷新译文" disabled={!book} onClick={() => {
            chapter.refetch();
            chapters.refetch();
            operations.refetch();
          }}><RefreshCw className="h-4 w-4" /></Button>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
            跟随本章最新译文
          </label>
          <span className="hidden sm:block h-4 border-l" />
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={selectable.length > 0 && selectedIndices.length === selectable.length}
              disabled={!selectable.length || retranslate.isPending}
              onChange={() => setSelected(selectedIndices.length === selectable.length ? new Set() : new Set(selectable.map((segment) => segment.index)))}
            />
            选择本章已译段落
          </label>
          <Button size="sm" disabled={!selectedIndices.length || retranslate.isPending} onClick={() => retranslate.mutate({ chapterIndex: index, indices: selectedIndices })}>
            {retranslate.isPending ? "提交中…" : "重译所选段落"}{selectedIndices.length > 0 && `（${selectedIndices.length}）`}
          </Button>
          {selectedIndices.length > 0 && <Button size="sm" variant="ghost" disabled={retranslate.isPending} onClick={() => setSelected(new Set())}>取消选择</Button>}
        </div>
        <p className="text-xs text-muted-foreground">
          可选择已完成的文本段落，按最新术语表重译，无需暂停全书翻译。重译会调用模型并计入用量；未完成段落继续等待后台翻译。
        </p>
      </div>
      <PageContainer className="space-y-4">
        <ErrorNotice error={project.error || chapters.error || chapter.error || operations.error || retranslate.error} />
        {!book && !project.isPending && <p className="text-sm text-muted-foreground">原文初始化后会显示段落，已完成的批次会自动出现。可先到翻译进度页启动或恢复任务。</p>}
        <RetranslationHistory operations={operations.data || []} chapterIndex={index} />
        {chapter.isPending && book && chapters.data?.length ? <p className="text-sm text-muted-foreground">正在加载章节…</p> : null}
        {chapter.data && <Card>
          <CardContent className="p-0">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b p-4">
              <h2 className="flex items-center gap-2 font-medium"><BookOpenText className="h-4 w-4" />{chapter.data.title_translated || chapter.data.title || `第 ${index + 1} 章`}</h2>
              <span className="text-xs text-muted-foreground">上次读取 {new Date(chapter.dataUpdatedAt).toLocaleTimeString()}</span>
            </div>
            <div className="hidden md:grid grid-cols-2 gap-4 border-b bg-muted/40 px-4 py-2 text-xs text-muted-foreground"><span>原文</span><span>当前译文</span></div>
            {segments.filter((segment) => segment.source.trim()).map((segment) => {
              const done = segment.target != null;
              const active = activeSegments.has(segment.index);
              const eligible = segment.kind === "text" && done && !active;
              return <article
                key={`${index}-${segment.index}`}
                data-testid={`reader-segment-${segment.index}`}
                ref={(node) => { if (node) paragraphRefs.current.set(segment.index, node); else paragraphRefs.current.delete(segment.index); }}
                className={cn("grid md:grid-cols-2 border-b last:border-0 scroll-mt-48", selected.has(segment.index) && "bg-primary/5")}
              >
                <div className="min-w-0 border-b md:border-b-0 md:border-r p-4">
                  <label className="mb-3 flex items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      aria-label={`选择第 ${segment.index + 1} 段`}
                      checked={selected.has(segment.index)}
                      disabled={!eligible || retranslate.isPending}
                      onChange={() => toggleSelection(segment.index)}
                    />
                    第 {segment.index + 1} 段
                    {segment.kind !== "text" && <span>· 非文本内容</span>}
                  </label>
                  <div className="mb-1 text-xs text-muted-foreground md:hidden">原文</div>
                  <p className="whitespace-pre-wrap break-words text-sm leading-7">{segment.source}</p>
                </div>
                <div className="min-w-0 p-4">
                  <div className="mb-1 text-xs text-muted-foreground md:hidden">当前译文</div>
                  {active && <Badge variant="info" className="mb-2">已提交重译，完成后更新</Badge>}
                  {done ? <p className="whitespace-pre-wrap break-words text-sm leading-7">{segment.target || <span className="text-muted-foreground">（译文为空）</span>}</p> : <p className="text-sm text-muted-foreground">等待翻译…</p>}
                </div>
              </article>;
            })}
            {!segments.length && <p className="p-6 text-sm text-muted-foreground">本章暂未载入段落。</p>}
          </CardContent>
        </Card>}
      </PageContainer>
      {glossaryOpen && <aside aria-label="阅读时编辑术语表" className="fixed inset-y-0 right-0 z-40 w-full max-w-2xl overflow-y-auto border-l bg-background shadow-2xl">
        <div className="sticky top-0 z-10 flex items-center justify-between border-b bg-background px-4 py-3">
          <p className="text-sm text-muted-foreground">保存术语后，返回阅读并选择需要更新的段落。</p>
          <Button variant="ghost" size="sm" aria-label="关闭术语表" onClick={() => setGlossaryOpen(false)}><X className="h-4 w-4" /></Button>
        </div>
        <GlossaryPage embedded />
      </aside>}
    </>
  );
}

function RetranslationHistory({ operations, chapterIndex }: { operations: Retranslation[]; chapterIndex: number }) {
  const records = operations.filter((operation) => operation.chapter_index === chapterIndex)
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
  if (!records.length) return null;
  const active = records.filter((operation) => pending(operation.status)).length;
  return <details className="rounded-md border bg-card p-4" open>
    <summary className="cursor-pointer text-sm font-medium">本章重译记录（{records.length}）{active > 0 && ` · ${active} 项进行中`}</summary>
    <ul className="mt-3 space-y-3">
      {records.map((operation) => <li key={operation.id} className="space-y-2 border-t pt-3 text-sm" data-testid={`retranslation-${operation.id}`}>
        <div className="flex flex-wrap gap-2 items-center">
          <Badge variant={operation.status === "done" ? "success" : operation.status === "error" ? "destructive" : operation.status === "conflict" ? "warning" : "secondary"}>
            {({ queued: "等待重译", running: "正在重译", done: "重译完成", error: "重译失败", conflict: "段落已有更新" })[operation.status] || operation.status}
          </Badge>
          <span>段落 {operation.segment_indices.map((value) => value + 1).join("、")}</span>
          <time className="text-xs text-muted-foreground">{new Date(operation.created_at).toLocaleString()}</time>
        </div>
        {operation.applied.length > 0 && <p className="text-muted-foreground">已更新段落：{operation.applied.map((value) => value + 1).join("、")}</p>}
        {operation.conflicts.length > 0 && <p className="text-amber-700 dark:text-amber-400">段落 {operation.conflicts.map((value) => value + 1).join("、")} 已有更新，保留当前译文。检查后可重新选择重译。</p>}
        {operation.error && <ErrorNotice error={operation.error} />}
      </li>)}
    </ul>
  </details>;
}
