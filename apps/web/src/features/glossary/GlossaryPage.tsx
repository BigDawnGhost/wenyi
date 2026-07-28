import { useCallback, useEffect, useRef, useState } from "react";
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
import {
  Plus, Lock, Unlock, Trash2, Pencil, Download, Upload, ChevronDown,
} from "lucide-react";

const TYPES = ["人物", "地名", "组织", "术语", "招式", "称谓", "敬称", "口癖", "固定表达", "拟声词"];

// ── CSV helpers ─────────────────────────────────────────────────────────
function parseCsv(text: string): Partial<Term>[] {
  const lines = text.trim().split("\n");
  if (lines.length < 2) return [];
  const headers = lines[0].split(",").map((h) => h.trim().replace(/^"|"$/g, ""));
  return lines.slice(1).map((line) => {
    const vals = line.split(",").map((v) => v.trim().replace(/^"|"$/g, ""));
    const row: Record<string, unknown> = {};
    headers.forEach((h, i) => { row[h] = vals[i] ?? ""; });
    return {
      source: String(row.source ?? ""),
      target: String(row.target ?? ""),
      reading: String(row.reading ?? ""),
      type: String(row.type ?? "术语"),
      gender: String(row.gender ?? ""),
      aliases: row.aliases ? String(row.aliases).split("|").filter(Boolean) : [],
      confidence: String(row.confidence ?? "medium"),
      locked: row.locked === "1" || row.locked === "true",
    };
  });
}

// ── Page ────────────────────────────────────────────────────────────────
export default function GlossaryPage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [editTerm, setEditTerm] = useState<Term | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [importOpen, setImportOpen] = useState(false);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const exportRef = useRef<HTMLDivElement>(null);

  const { data: terms } = useQuery({
    queryKey: ["terms", pid, q, type],
    queryFn: () => api.listTerms(pid, { q: q || undefined, type: type || undefined }),
    enabled: !!pid,
  });
  const { data: conflicts } = useQuery({ queryKey: ["conflicts", pid], queryFn: () => api.listConflicts(pid), enabled: !!pid });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["terms", pid] });
    qc.invalidateQueries({ queryKey: ["conflicts", pid] });
    setSelected(new Set());
  };
  const lock = useMutation({ mutationFn: ({ s, lock }: { s: string; lock: boolean }) => lock ? api.unlockTerm(pid, s) : api.lockTerm(pid, s), onSuccess: invalidate });
  const del = useMutation({ mutationFn: (s: string) => api.deleteTerm(pid, s), onSuccess: () => { invalidate(); toast.success("已删除"); } });
  const resolve = useMutation({ mutationFn: ({ cid, decision, target }: { cid: number; decision: string; target?: string }) => api.resolveConflict(pid, cid, { decision, target }), onSuccess: () => { invalidate(); toast.success("已解决冲突"); } });

  // batch ops
  const batchLock = useMutation({
    mutationFn: (lockVal: boolean) => Promise.all([...selected].map((s) => lockVal ? api.unlockTerm(pid, s) : api.lockTerm(pid, s))),
    onSuccess: () => { invalidate(); toast.success("批量操作完成"); },
    onError: () => toast.error("批量操作失败"),
  });
  const batchDelete = useMutation({
    mutationFn: () => Promise.all([...selected].map((s) => api.deleteTerm(pid, s))),
    onSuccess: () => { invalidate(); toast.success("批量删除完成"); },
    onError: () => toast.error("批量删除失败"),
  });

  const toggleSelect = (source: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(source)) next.delete(source);
      else next.add(source);
      return next;
    });
  };
  const toggleSelectAll = () => {
    if (!terms) return;
    if (selected.size === terms.length) setSelected(new Set());
    else setSelected(new Set(terms.map((t) => t.source)));
  };

  // export
  const handleExport = (fmt: "json" | "csv") => {
    setExportMenuOpen(false);
    window.open(api.exportGlossaryUrl(pid, fmt), "_blank");
  };

  // close export menu on outside click
  const handleExportBlur = useCallback((e: React.FocusEvent) => {
    if (exportRef.current && !exportRef.current.contains(e.relatedTarget as Node)) {
      setExportMenuOpen(false);
    }
  }, []);

  const allLocked = terms && terms.length > 0 && [...selected].every((s) => terms.find((t) => t.source === s)?.locked);

  return (
    <>
      <PageHeader
        title="术语表"
        subtitle="专有名词对照表，锁定后翻译将严格遵循"
        actions={
          <div className="flex items-center gap-2">
            <div className="relative" ref={exportRef} onBlur={handleExportBlur}>
              <Button variant="outline" onClick={() => setExportMenuOpen(!exportMenuOpen)}>
                <Download className="h-4 w-4" /> 导出 <ChevronDown className="h-3 w-3" />
              </Button>
              {exportMenuOpen && (
                <div className="absolute right-0 top-full z-10 mt-1 w-36 rounded-md border bg-popover p-1 shadow-md">
                  <button className="w-full rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent" onClick={() => handleExport("csv")}>CSV 文件</button>
                  <button className="w-full rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent" onClick={() => handleExport("json")}>JSON 文件</button>
                </div>
              )}
            </div>
            <Button variant="outline" onClick={() => setImportOpen(true)}><Upload className="h-4 w-4" /> 导入</Button>
            <Button onClick={() => setAddOpen(true)}><Plus className="h-4 w-4" /> 添加术语</Button>
          </div>
        }
      />
      <PageContainer className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Input placeholder="搜索源词 / 译词 / 别名…" value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
          <Select value={type} onChange={(e) => setType(e.target.value)} className="max-w-[140px]">
            <option value="">全部类型</option>
            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select>
        </div>

        {/* batch action bar */}
        {selected.size > 0 && (
          <div className="flex items-center gap-3 rounded-md border bg-muted/60 px-4 py-2 text-sm">
            <span>已选 {selected.size} 项</span>
            <Button size="sm" variant="outline" onClick={() => batchLock.mutate(!!allLocked)}>
              {allLocked ? <><Unlock className="h-3.5 w-3.5" /> 批量解锁</> : <><Lock className="h-3.5 w-3.5" /> 批量锁定</>}
            </Button>
            <Button size="sm" variant="destructive" onClick={() => { if (confirm(`确认删除 ${selected.size} 条术语？`)) batchDelete.mutate(); }}>
              <Trash2 className="h-3.5 w-3.5" /> 批量删除
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>取消选择</Button>
          </div>
        )}

        <Card>
          <CardContent className="p-0">
            <table className="w-full text-sm">
              <thead className="border-b text-xs text-muted-foreground">
                <tr>
                  <th className="text-left p-3 font-medium w-10">
                    <input type="checkbox" checked={terms ? selected.size === terms.length && terms.length > 0 : false} onChange={toggleSelectAll} />
                  </th>
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
                    <td className="p-3">
                      <input type="checkbox" checked={selected.has(t.source)} onChange={() => toggleSelect(t.source)} />
                    </td>
                    <td className="p-3 font-medium">{t.source}</td>
                    <td className="p-3">{t.target}</td>
                    <td className="p-3 text-muted-foreground">{t.reading || "—"}</td>
                    <td className="p-3"><Badge variant="outline">{t.type}</Badge></td>
                    <td className="p-3">{t.confidence === "high" ? <Badge variant="success">高</Badge> : t.confidence === "low" ? <Badge variant="warning">低</Badge> : <Badge variant="secondary">中</Badge>}</td>
                    <td className="p-3">{t.locked ? <Lock className="h-3.5 w-3.5" /> : <span className="text-muted-foreground">—</span>}</td>
                    <td className="p-3 text-right">
                      <Button variant="ghost" size="sm" onClick={() => setEditTerm(t)}><Pencil className="h-3.5 w-3.5" /></Button>
                      <Button variant="ghost" size="sm" onClick={() => lock.mutate({ s: t.source, lock: !!t.locked })}>
                        {t.locked ? <><Unlock className="h-3.5 w-3.5" /></> : <><Lock className="h-3.5 w-3.5" /></>}
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => del.mutate(t.source)}><Trash2 className="h-3.5 w-3.5" /></Button>
                    </td>
                  </tr>
                ))}
                {terms && terms.length === 0 && <tr><td colSpan={8} className="p-8 text-center text-muted-foreground text-sm">暂无术语。翻译过程中会自动提取。</td></tr>}
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
        <EditTermDialog pid={pid} term={editTerm} onClose={() => setEditTerm(null)} onSaved={() => { invalidate(); setEditTerm(null); }} />
        <ImportDialog pid={pid} existingTerms={terms || []} open={importOpen} onClose={() => setImportOpen(false)} onSaved={() => { invalidate(); setImportOpen(false); }} />
      </PageContainer>
    </>
  );
}

// ── Add Term Dialog ─────────────────────────────────────────────────────
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

// ── Edit Term Dialog ────────────────────────────────────────────────────
function EditTermDialog({ pid, term, onClose, onSaved }: { pid: string; term: Term | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ source: "", target: "", reading: "", type: "术语", note: "", gender: "", confidence: "medium", aliases: "" });

  useEffect(() => {
    if (term) {
      setForm({
        source: term.source,
        target: term.target,
        reading: term.reading || "",
        type: term.type || "术语",
        note: term.note || "",
        gender: term.gender || "",
        confidence: term.confidence || "medium",
        aliases: (term.aliases || []).join(", "),
      });
    }
  }, [term]);

  const m = useMutation({
    mutationFn: () => api.updateTerm(pid, term!.source, {
      ...form,
      aliases: form.aliases.split(",").map((s) => s.trim()).filter(Boolean),
    }),
    onSuccess: () => { toast.success("已更新"); onSaved(); },
    onError: (e) => toast.error(`更新失败：${(e as Error).message}`),
  });

  if (!term) return null;

  return (
    <Dialog open={!!term} onClose={onClose}>
      <div className="text-lg font-semibold mb-4">编辑术语</div>
      <div className="space-y-3">
        <div><Label>源词 *</Label><Input value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} /></div>
        <div><Label>译词 *</Label><Input value={form.target} onChange={(e) => setForm({ ...form, target: e.target.value })} /></div>
        <div className="grid grid-cols-2 gap-3">
          <div><Label>读音</Label><Input value={form.reading} onChange={(e) => setForm({ ...form, reading: e.target.value })} /></div>
          <div><Label>类型</Label><Select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>{TYPES.map((t) => <option key={t} value={t}>{t}</option>)}</Select></div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div><Label>性别</Label><Input value={form.gender} onChange={(e) => setForm({ ...form, gender: e.target.value })} /></div>
          <div><Label>置信度</Label><Select value={form.confidence} onChange={(e) => setForm({ ...form, confidence: e.target.value })}>
            <option value="high">高</option>
            <option value="medium">中</option>
            <option value="low">低</option>
          </Select></div>
        </div>
        <div><Label>别名（逗号分隔）</Label><Input value={form.aliases} onChange={(e) => setForm({ ...form, aliases: e.target.value })} placeholder="别名1, 别名2" /></div>
        <div><Label>备注</Label><Textarea value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} /></div>
      </div>
      <div className="flex justify-end gap-2 mt-4">
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button onClick={() => m.mutate()} disabled={!form.source || !form.target || m.isPending}>保存</Button>
      </div>
    </Dialog>
  );
}

// ── Import Dialog ───────────────────────────────────────────────────────
interface ImportConflict {
  source: string;
  existingTarget: string;
  newTarget: string;
  decision: "skip" | "overwrite";
}

function ImportDialog({ pid, existingTerms, open, onClose, onSaved }: {
  pid: string; existingTerms: Term[]; open: boolean; onClose: () => void; onSaved: () => void;
}) {
  const [step, setStep] = useState<"upload" | "conflicts" | "importing">("upload");
  const [parsed, setParsed] = useState<Partial<Term>[]>([]);
  const [conflicts, setConflicts] = useState<ImportConflict[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const reset = () => { setStep("upload"); setParsed([]); setConflicts([]); };

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result);
      let items: Partial<Term>[] = [];
      if (file.name.endsWith(".csv")) {
        items = parseCsv(text);
      } else {
        try { items = JSON.parse(text); } catch { toast.error("JSON 解析失败"); return; }
      }
      if (items.length === 0) { toast.error("文件为空或格式不正确"); return; }
      // detect conflicts
      const existingMap = new Map(existingTerms.map((t) => [t.source, t.target]));
      const found: ImportConflict[] = [];
      for (const it of items) {
        if (it.source && existingMap.has(it.source)) {
          found.push({
            source: it.source,
            existingTarget: existingMap.get(it.source)!,
            newTarget: it.target || "",
            decision: "overwrite",
          });
        }
      }
      setParsed(items);
      if (found.length > 0) {
        setConflicts(found);
        setStep("conflicts");
      } else {
        doImport(items);
      }
    };
    reader.readAsText(file);
  };

  const doImport = (items?: Partial<Term>[]) => {
    setStep("importing");
    const toImport = items || parsed;
    // apply conflict decisions: filter out skipped
    const skipSources = new Set(conflicts.filter((c) => c.decision === "skip").map((c) => c.source));
    const filtered = toImport.filter((t) => !skipSources.has(t.source!));
    m.mutate(filtered);
  };

  const m = useMutation({
    mutationFn: (items: Partial<Term>[]) => api.importGlossary(pid, items),
    onSuccess: (res) => { toast.success(`成功导入 ${res.imported} 条术语`); reset(); onSaved(); },
    onError: (e) => { toast.error(`导入失败：${(e as Error).message}`); setStep("upload"); },
  });

  const toggleConflict = (idx: number) => {
    setConflicts((prev) => prev.map((c, i) => i === idx ? { ...c, decision: c.decision === "skip" ? "overwrite" : "skip" } : c));
  };

  return (
    <Dialog open={open} onClose={() => { reset(); onClose(); }}>
      <div className="text-lg font-semibold mb-4">导入术语</div>

      {step === "upload" && (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">选择 CSV 或 JSON 文件导入术语。CSV 需包含 source, target 等列头。</p>
          <input ref={fileRef} type="file" accept=".csv,.json" className="hidden" onChange={handleFile} />
          <Button onClick={() => fileRef.current?.click()}><Upload className="h-4 w-4" /> 选择文件</Button>
        </div>
      )}

      {step === "conflicts" && (
        <div className="space-y-4">
          <p className="text-sm">发现 {conflicts.length} 条冲突术语，请选择处理方式：</p>
          <div className="max-h-60 overflow-auto space-y-2">
            {conflicts.map((c, i) => (
              <div key={c.source} className="flex items-center gap-3 rounded border p-2 text-sm">
                <span className="font-medium min-w-[80px]">{c.source}</span>
                <span className="text-muted-foreground">当前：{c.existingTarget}</span>
                <span className="text-muted-foreground">导入：{c.newTarget}</span>
                <button
                  className={`ml-auto rounded px-2 py-0.5 text-xs ${c.decision === "overwrite" ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"}`}
                  onClick={() => toggleConflict(i)}
                >
                  {c.decision === "overwrite" ? "覆盖" : "跳过"}
                </button>
              </div>
            ))}
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => { reset(); onClose(); }}>取消</Button>
            <Button onClick={() => doImport()}>确认导入</Button>
          </div>
        </div>
      )}

      {step === "importing" && (
        <div className="py-8 text-center text-sm text-muted-foreground">导入中…</div>
      )}
    </Dialog>
  );
}
