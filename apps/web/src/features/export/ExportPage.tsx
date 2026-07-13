import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, Label } from "@/components/ui/form";
import { cn, formatBytes } from "@/lib/utils";
import { Download, Share2 } from "lucide-react";

const FORMATS = [
  { id: "epub", name: "EPUB", desc: "保持原书排版" },
  { id: "txt", name: "TXT", desc: "纯文本" },
];

export default function ExportPage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [fmt, setFmt] = useState("epub");
  const [bilingual, setBilingual] = useState(false);
  const [order, setOrder] = useState("target_first");
  const [about, setAbout] = useState(true);

  const { data: exports } = useQuery({ queryKey: ["exports", pid], queryFn: () => api.listExports(pid), enabled: !!pid, refetchInterval: (q) => (q.state.data?.some((e) => e.status === "pending") ? 3000 : false) });

  const create = useMutation({
    mutationFn: () => api.createExport(pid, { format: fmt, bilingual, order, about_page: about }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["exports", pid] }); toast.success("已开始生成导出"); },
    onError: (e) => toast.error(`导出失败：${(e as Error).message}`),
  });

  return (
    <>
      <PageHeader title="导出" subtitle="生成译文文件（EPUB / TXT）" />
      <PageContainer className="space-y-4 max-w-3xl">
        <Card>
          <CardContent className="p-4 space-y-4">
            <div>
              <Label>格式</Label>
              <div className="grid grid-cols-2 gap-2 mt-2">
                {FORMATS.map((f) => (
                  <button key={f.id} onClick={() => setFmt(f.id)} className={cn("rounded-lg border p-3 text-left transition-colors", fmt === f.id ? "border-primary ring-1 ring-primary" : "hover:border-primary/40")}>
                    <div className="font-medium text-sm">{f.name}</div>
                    <div className="text-xs text-muted-foreground">{f.desc}</div>
                  </button>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-6 pt-2">
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!bilingual} onChange={() => setBilingual(false)} /> 单语版</label>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={bilingual} onChange={() => setBilingual(true)} /> 双语版</label>
              {bilingual && (
                <Select value={order} onChange={(e) => setOrder(e.target.value)} className="max-w-[160px]">
                  <option value="target_first">译文在上</option>
                  <option value="source_first">原文在上</option>
                </Select>
              )}
            </div>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={about} onChange={(e) => setAbout(e.target.checked)} /> 附加"关于此翻译"说明页</label>
            <Button onClick={() => create.mutate()} disabled={create.isPending}><Download className="h-4 w-4" /> 导出</Button>
          </CardContent>
        </Card>

        {exports && exports.length > 0 && (
          <Card>
            <CardContent className="p-0">
              <table className="w-full text-sm">
                <thead className="border-b text-xs text-muted-foreground"><tr>{["格式", "文件大小", "状态", "操作"].map((h) => <th key={h} className="text-left p-3 font-medium">{h}</th>)}</tr></thead>
                <tbody>
                  {exports.map((e) => (
                    <tr key={e.id} className="border-b last:border-0">
                      <td className="p-3 font-medium">{e.format.toUpperCase()}</td>
                      <td className="p-3 text-muted-foreground">{formatBytes(e.size)}</td>
                      <td className="p-3">{e.status === "done" ? <Badge variant="success">完成</Badge> : e.status === "error" ? <Badge variant="destructive">失败</Badge> : <Badge variant="secondary">生成中</Badge>}</td>
                      <td className="p-3">
                        {e.status === "done" && (
                          <div className="flex gap-2">
                            <a href={api.downloadExportUrl(pid, e.id)} download><Button size="sm" variant="outline"><Download className="h-3.5 w-3.5" /> 下载</Button></a>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        )}
      </PageContainer>
    </>
  );
}

void Share2;
