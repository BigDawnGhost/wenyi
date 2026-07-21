import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type AnalysisPayload } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/form";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/misc";

export default function StylePage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [tab, setTab] = useState("style");
  const { data } = useQuery({ queryKey: ["analysis", pid], queryFn: () => api.getAnalysis(pid), enabled: !!pid });

  const analysis = (data?.analysis || {}) as Record<string, unknown>;
  const characters = (analysis.characters as Record<string, string>[]) || [];
  const styleGuide = String(analysis.style_guide || "");
  const synopsis = String(analysis.book_synopsis || "");
  const digests = data?.chapter_digests || [];

  const save = useMutation({
    mutationFn: (a: Record<string, unknown>) => api.updateAnalysis(pid, a),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["analysis", pid] }); toast.success("已保存"); },
  });

  const [guideDraft, setGuideDraft] = useState<string>(styleGuide);
  const [synopsisDraft, setSynopsisDraft] = useState<string>(synopsis);
  // 同步外部数据到 draft
  if (styleGuide && !guideDraft) setGuideDraft(styleGuide);
  if (synopsis && !synopsisDraft) setSynopsisDraft(synopsis);

  return (
    <>
      <PageHeader title="风格 & 概要" subtitle="翻译前准备的产物，可编辑以影响后续翻译" />
      <PageContainer>
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="style">风格分析</TabsTrigger>
            <TabsTrigger value="characters">角色列表</TabsTrigger>
            <TabsTrigger value="synopsis">书籍概要</TabsTrigger>
            <TabsTrigger value="digests">章节摘要</TabsTrigger>
          </TabsList>

          <TabsContent value="style" className="mt-4 space-y-4">
            <Card>
              <CardHeader><CardTitle>风格概览</CardTitle></CardHeader>
              <CardContent>
                <div className="grid md:grid-cols-3 gap-4 text-sm">
                  {([
                    ["体裁", analysis.genre], ["语调", analysis.tone], ["叙事", analysis.narration],
                    ["节奏", analysis.pacing], ["对话风格", analysis.dialogue_style], ["修辞", analysis.rhetoric],
                  ] as [string, unknown][]).map(([k, v]) => (
                    <div key={k}>
                      <div className="text-xs text-muted-foreground">{k}</div>
                      <div className="font-medium">{String(v ?? "") || "—"}</div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>风格指南</CardTitle>
                <Button size="sm" onClick={() => save.mutate({ ...analysis, style_guide: guideDraft })} disabled={save.isPending}>保存</Button>
              </CardHeader>
              <CardContent>
                <Textarea className="min-h-[160px]" value={guideDraft} onChange={(e) => setGuideDraft(e.target.value)} />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="characters" className="mt-4">
            <Card><CardContent className="p-0">
              <table className="w-full text-sm">
                <thead className="border-b text-xs text-muted-foreground"><tr>{["角色名", "译名", "描述", "性别", "首次出场"].map((h) => <th key={h} className="text-left p-3 font-medium">{h}</th>)}</tr></thead>
                <tbody>
                  {characters.map((c, i) => (
                    <tr key={i} className="border-b last:border-0">
                      <td className="p-3 font-medium">{c.source}</td>
                      <td className="p-3">{c.target}</td>
                      <td className="p-3 text-muted-foreground">{c.note || "—"}</td>
                      <td className="p-3">{c.gender || "—"}</td>
                      <td className="p-3 text-muted-foreground">{c.first_seen ?? "—"}</td>
                    </tr>
                  ))}
                  {characters.length === 0 && <tr><td colSpan={5} className="p-8 text-center text-muted-foreground text-sm">尚无角色数据（需开启风格分析并完成准备阶段）。</td></tr>}
                </tbody>
              </table>
            </CardContent></Card>
          </TabsContent>

          <TabsContent value="synopsis" className="mt-4">
            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>全书概要</CardTitle>
                <Button size="sm" onClick={() => save.mutate({ ...analysis, book_synopsis: synopsisDraft })} disabled={save.isPending}>保存</Button>
              </CardHeader>
              <CardContent>
                <Textarea className="min-h-[220px]" value={synopsisDraft} onChange={(e) => setSynopsisDraft(e.target.value)} />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="digests" className="mt-4">
            <Card><CardContent className="p-0">
              <table className="w-full text-sm">
                <thead className="border-b text-xs text-muted-foreground"><tr><th className="text-left p-3 font-medium">章节</th><th className="text-left p-3 font-medium">摘要</th></tr></thead>
                <tbody>
                  {digests.map((d) => (
                    <tr key={d.index} className="border-b last:border-0">
                      <td className="p-3 align-top whitespace-nowrap font-medium">{d.title || `第 ${d.index + 1} 章`}</td>
                      <td className="p-3 text-muted-foreground">{d.digest || "—"}</td>
                    </tr>
                  ))}
                  {digests.length === 0 && <tr><td colSpan={2} className="p-8 text-center text-muted-foreground text-sm">尚无章节摘要（需开启书籍预理解）。</td></tr>}
                </tbody>
              </table>
            </CardContent></Card>
          </TabsContent>
        </Tabs>
      </PageContainer>
    </>
  );
}

// 避免未用 import 报错
void (null as unknown as AnalysisPayload);
