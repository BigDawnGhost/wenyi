import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type Term } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Input, Select, Textarea, Label } from "@/components/ui/form";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/misc";
import { Plus, Lock, Unlock, Trash2 } from "lucide-react";

const TYPES = ["人物", "地名", "组织", "术语", "招式", "称谓", "敬称", "口癖", "固定表达", "拟声词"];

export default function GlossaryPage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [addOpen, setAddOpen] = useState(false);

  const { data: terms } = useQuery({
    queryKey: ["terms", pid, q, type],
    queryFn: () => api.listTerms(pid, { q: q || undefined, type: type || undefined }),
    enabled: !!pid,
  });
  const { data: conflicts } = useQuery({ queryKey: ["conflicts", pid], queryFn: () => api.listConflicts(pid), enabled: !!pid });

  const invalidate = () => { qc.invalidateQueries({ queryKey: ["terms", pid] }); qc.invalidateQueries({ queryKey: ["conflicts", pid] }); };
  const lock = useMutation({ mutationFn: ({ s, lock }: { s: string; lock: boolean }) => lock ? api.unlockTerm(pid, s) : api.lockTerm(pid, s), onSuccess: invalidate });
  const del = useMutation({ mutationFn: (s: string) => api.deleteTerm(pid, s), onSuccess: () => { invalidate(); toast.success("已删除"); } });
  const resolve = useMutation({ mutationFn: ({ cid, decision, target }: { cid: number; decision: string; target?: string }) => api.resolveConflict(pid, cid, { decision, target }), onSuccess: () => { invalidate(); toast.success("已解决冲突"); } });

  return (
    <>
      <PageHeader title="术语表" subtitle="专有名词对照表，锁定后翻译将严格遵循" actions={<Button onClick={() => setAddOpen(true)}><Plus className="h-4 w-4" /> 添加术语</Button>} />
      <PageContainer className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Input placeholder="搜索源词 / 译词 / 别名…" value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
          <Select value={type} onChange={(e) => setType(e.target.value)} className="max-w-[140px]">
            <option value="">全部类型</option>
            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select>
        </div>

        <Card>
          <CardContent className="p-0">
            <table className="w-full text-sm">
              <thead className="border-b text-xs text-muted-foreground">
                <tr>
                  <th className="text-left p-3 font-medium">源词</th>
                  <th className="text-left p-3 font-medium">译词</th>
                  <th className="text-left p-3 font-medium">读音</th>
                  <th className="text-left p-3 font-medium">类型</th>
                  <th className="text-left p-3 font-medium">置信度</th>
                  <th className="text-left p-3 font-medium">锁定</th>
                  <th className="text-right p-3 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {(terms || []).map((t: Term) => (
                  <tr key={t.source} className="border-b last:border-0 hover:bg-muted/40">
                    <td className="p-3 font-medium">{t.source}</td>
                    <td className="p-3">{t.target}</td>
                    <td className="p-3 text-muted-foreground">{t.reading || "—"}</td>
                    <td className="p-3"><Badge variant="outline">{t.type}</Badge></td>
                    <td className="p-3">{t.confidence === "high" ? <Badge variant="success">高</Badge> : t.confidence === "low" ? <Badge variant="warning">低</Badge> : <Badge variant="secondary">中</Badge>}</td>
                    <td className="p-3">{t.locked ? <Lock className="h-3.5 w-3.5" /> : <span className="text-muted-foreground">—</span>}</td>
                    <td className="p-3 text-right">
                      <Button variant="ghost" size="sm" onClick={() => lock.mutate({ s: t.source, lock: !!t.locked })}>
                        {t.locked ? <><Unlock className="h-3.5 w-3.5" /> 解锁</> : <><Lock className="h-3.5 w-3.5" /> 锁定</>}
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => del.mutate(t.source)}><Trash2 className="h-3.5 w-3.5" /></Button>
                    </td>
                  </tr>
                ))}
                {terms && terms.length === 0 && <tr><td colSpan={7} className="p-8 text-center text-muted-foreground text-sm">暂无术语。翻译过程中会自动提取。</td></tr>}
              </tbody>
            </table>
          </CardContent>
        </Card>

        {conflicts && conflicts.length > 0 && (
          <Card className="border-amber-300 bg-amber-50 dark:bg-amber-950/20">
            <CardContent className="p-4 space-y-2">
              <div className="font-medium text-sm">待解决冲突（{conflicts.length}）</div>
              {conflicts.map((c) => (
                <div key={c.id} className="flex flex-wrap items-center gap-3 text-sm border-t pt-2">
                  <span className="font-medium">{c.source}</span>
                  <span className="text-muted-foreground">当前：{c.existing_target}</span>
                  <span className="text-muted-foreground">AI 提议：{c.proposed_target}</span>
                  <span className="flex gap-1 ml-auto">
                    <Button size="sm" variant="outline" onClick={() => resolve.mutate({ cid: c.id, decision: "current" })}>采纳当前</Button>
                    <Button size="sm" variant="outline" onClick={() => resolve.mutate({ cid: c.id, decision: "proposed" })}>采纳提议</Button>
                  </span>
                </div>
              ))}
            </CardContent>
          </Card>
        )}

        <AddTermDialog pid={pid} open={addOpen} onClose={() => setAddOpen(false)} onSaved={() => { invalidate(); setAddOpen(false); }} />
      </PageContainer>
    </>
  );
}

function AddTermDialog({ pid, open, onClose, onSaved }: { pid: string; open: boolean; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ source: "", target: "", reading: "", type: "术语", note: "" });
  const m = useMutation({
    mutationFn: () => api.addTerm(pid, form),
    onSuccess: () => { toast.success("已添加"); setForm({ source: "", target: "", reading: "", type: "术语", note: "" }); onSaved(); },
    onError: (e) => toast.error(`添加失败：${(e as Error).message}`),
  });
  return (
    <Dialog open={open} onClose={onClose}>
      <div className="text-lg font-semibold mb-4">添加术语</div>
      <div className="space-y-3">
        <div><Label>源词 *</Label><Input value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} /></div>
        <div><Label>译词 *</Label><Input value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })} /></div>
        <div className="grid grid-cols-2 gap-3">
          <div><Label>读音</Label><Input value={form.reading} onChange={(e) => setForm({ ...form, reading: e.target.value })} /></div>
          <div><Label>类型</Label><Select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>{TYPES.map((t) => <option key={t} value={t}>{t}</option>)}</Select></div>
        </div>
        <div><Label>备注</Label><Textarea value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} /></div>
      </div>
      <div className="flex justify-end gap-2 mt-4">
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button onClick={() => m.mutate()} disabled={!form.source || !form.target || m.isPending}>添加</Button>
      </div>
    </Dialog>
  );
}
