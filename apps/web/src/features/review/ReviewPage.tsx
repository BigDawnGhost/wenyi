import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ChevronLeft, ChevronRight, Check } from "lucide-react";

export default function ReviewPage() {
  const { pid = "", ci } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const current = ci !== undefined ? Number(ci) : undefined;
  const [activeCi, setActiveCi] = useState<number | undefined>(current);

  const { data: chapters } = useQuery({ queryKey: ["chapters", pid], queryFn: () => api.listChapters(pid), enabled: !!pid });
  const reviewable = (chapters || []).filter((c) => c.status === "done");
  const idx = activeCi ?? reviewable[0]?.index;

  const { data: chapter } = useQuery({ queryKey: ["review", pid, idx], queryFn: () => api.getReview(pid, idx!), enabled: idx !== undefined });

  const edit = useMutation({
    mutationFn: ({ segIdx, target }: { segIdx: number; target: string }) => api.editSegment(pid, idx!, segIdx, target),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["review", pid, idx] }),
  });
  const complete = useMutation({
    mutationFn: () => api.markReviewComplete(pid, idx!),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["chapters", pid] }); toast.success("已标记审校完成"); },
  });

  const go = (delta: number) => {
    if (!reviewable.length || idx === undefined) return;
    const pos = reviewable.findIndex((c) => c.index === idx);
    const next = reviewable[pos + delta];
    if (next) { setActiveCi(next.index); navigate(`/projects/${pid}/review/${next.index}`); }
  };

  const issues = (chapter?.review_issues || []) as Record<string, unknown>[];

  return (
    <>
      <PageHeader
        title={`审校${chapter ? ` — ${chapter.title_translated || chapter.title}` : ""}`}
        subtitle="左原文 / 右译文，点击译文可直接编辑"
        actions={
          <>
            <Button variant="outline" onClick={() => go(-1)}><ChevronLeft className="h-4 w-4" /> 上一章</Button>
            <Button variant="outline" onClick={() => go(1)}>下一章 <ChevronRight className="h-4 w-4" /></Button>
            <Button onClick={() => complete.mutate()} disabled={complete.isPending || idx === undefined}><Check className="h-4 w-4" /> 标记审校完成</Button>
          </>
        }
      />
      <PageContainer>
        {idx === undefined ? (
          <Card><CardContent className="py-16 text-center text-muted-foreground text-sm">暂无可审校的章节（需先完成翻译）。</CardContent></Card>
        ) : (
          <div className="grid lg:grid-cols-[1fr_320px] gap-4">
            <Card>
              <CardContent className="p-0">
                <div className="grid md:grid-cols-2">
                  <div className="border-r">
                    <div className="px-3 py-2 text-xs font-medium text-muted-foreground border-b">原文</div>
                    <div className="max-h-[70vh] overflow-auto">
                      {(chapter?.segments || []).filter((s) => s.source?.trim()).map((s) => (
                        <div key={`s-${s.index}`} className="px-3 py-2 border-b last:border-0 text-sm whitespace-pre-wrap">{s.source}</div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="px-3 py-2 text-xs font-medium text-muted-foreground border-b">译文</div>
                    <div className="max-h-[70vh] overflow-auto">
                      {(chapter?.segments || []).filter((s) => s.source?.trim()).map((s) => (
                        <EditableSegment key={`t-${s.index}`} index={s.index} value={s.target || ""} onSave={(v) => edit.mutate({ segIdx: s.index, target: v })} />
                      ))}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
            <Card className="h-fit">
              <CardContent className="p-4 space-y-3">
                <div className="font-medium text-sm">AI 审校意见</div>
                {issues.length === 0 ? <p className="text-sm text-muted-foreground">无问题。</p> : issues.map((it, i) => (
                  <div key={i} className="rounded border border-amber-200 bg-amber-50 dark:bg-amber-950/20 p-3 text-sm">
                    <Badge variant="warning">{String(it.type || "问题")}</Badge>
                    <div className="mt-1">{String(it.detail || "")}</div>
                    {it.suggestion ? <div className="mt-1 text-muted-foreground">建议：{String(it.suggestion)}</div> : null}
                    <div className="mt-1 text-xs text-muted-foreground">段 #{Number(it.index ?? 0) + 1}</div>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>
        )}
      </PageContainer>
    </>
  );
}

function EditableSegment({ index, value, onSave }: { index: number; value: string; onSave: (v: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  if (!editing) {
    return (
      <div className="px-3 py-2 border-b last:border-0 text-sm whitespace-pre-wrap cursor-text hover:bg-accent/40" onClick={() => { setDraft(value); setEditing(true); }}>
        {value || <span className="text-muted-foreground">（空，点击编辑）</span>}
      </div>
    );
  }
  return (
    <div className="px-3 py-2 border-b last:border-0">
      <textarea className="w-full min-h-[60px] rounded border bg-background p-2 text-sm" value={draft} onChange={(e) => setDraft(e.target.value)} />
      <div className="flex gap-1 mt-1">
        <Button size="sm" onClick={() => { onSave(draft); setEditing(false); toast.success(`已更新段 #${index + 1}`); }}>保存</Button>
        <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>取消</Button>
      </div>
    </div>
  );
}
