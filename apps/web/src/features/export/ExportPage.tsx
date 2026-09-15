import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type ExportFormat, type PdfEngine } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, Label } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { cn, formatBytes } from "@/lib/utils";

export default function ExportPage() {
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [defaultsLoaded, setDefaultsLoaded] = useState(false);
  const { data: config } = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const [format, setFormat] = useState<ExportFormat | "">("");
  const [bilingual, setBilingual] = useState(false);
  const [order, setOrder] = useState<"target_first" | "source_first">(
    "target_first",
  );
  const [about, setAbout] = useState(true);
  const [preserveStyle, setPreserveStyle] = useState(false);
  const [pdfBackend, setPdfBackend] = useState<PdfEngine | "">("");
  const { data: project, error: projectError } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
  });
  const { data: caps, error: capsError } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
  });
  const { data: exports, error: exportsError } = useQuery({
    queryKey: ["exports", pid],
    queryFn: () => api.listExports(pid),
    refetchInterval: (q) =>
      q.state.data?.some((e) =>
        ["pending", "running", "queued"].includes(e.status),
      )
        ? 2000
        : false,
  });
  useEffect(() => {
    if (config && !defaultsLoaded) {
      const output = (config.effective.output || {}) as Record<string, unknown>;
      setBilingual(Boolean(output.bilingual));
      setAbout(output.about_page !== false);
      setPreserveStyle(Boolean(output.bilingual_preserve_source_style));
      setOrder(
        output.bilingual_order === "source_first"
          ? "source_first"
          : "target_first",
      );
      setDefaultsLoaded(true);
    }
  }, [config, defaultsLoaded]);
  const subtitle = project?.fmt === "srt";
  const formats: ExportFormat[] = subtitle
    ? ["srt"]
    : ((caps?.output_formats || []).filter(
        (f) => f !== "srt",
      ) as ExportFormat[]);
  const fmt = subtitle ? "srt" : format;
  const create = useMutation({
    mutationFn: () =>
      api.createExport(pid, {
        format: fmt || undefined,
        bilingual,
        order,
        about_page: subtitle ? false : about,
        preserve_source_style: preserveStyle,
        ...(pdfBackend && fmt === "pdf" ? { pdf_engine: pdfBackend } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["exports", pid] });
      toast.success("导出任务已提交");
    },
  });
  const download = useMutation({
    mutationFn: (id: number) => api.downloadExport(pid, id),
  });

  return (
    <>
      <PageHeader
        title="导出译文"
        subtitle="从已保存译文生成文件；执行中的翻译可同时导出已保存快照"
      />
      <PageContainer className="space-y-4 max-w-4xl">
        <ErrorNotice
          error={
            projectError ||
            capsError ||
            exportsError ||
            create.error ||
            download.error
          }
        />
        <Card>
          <CardContent className="p-5 space-y-4">
            <div>
              <Label>输出格式</Label>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2">
                {!subtitle && (
                  <button
                    onClick={() => setFormat("")}
                    className={cn(
                      "rounded border p-3 text-left text-sm",
                      !fmt && "border-primary ring-1 ring-primary",
                    )}
                  >
                    <strong>自动选择</strong>
                    <p className="text-xs text-muted-foreground mt-1">
                      依据原文格式和 PDF 解析后端
                    </p>
                  </button>
                )}
                {formats.map((f) => (
                  <button
                    key={f}
                    onClick={() => setFormat(f)}
                    className={cn(
                      "rounded border p-3 text-left text-sm",
                      fmt === f && "border-primary ring-1 ring-primary",
                    )}
                  >
                    {f.toUpperCase()}
                  </button>
                ))}
              </div>
            </div>
            {fmt === "pdf" && (
              <div>
                <Label htmlFor="pdf-export-engine">PDF 导出引擎</Label>
                <Select
                  id="pdf-export-engine"
                  value={pdfBackend}
                  onChange={(e) =>
                    setPdfBackend(e.target.value as PdfEngine | "")
                  }
                  className="mt-2"
                >
                  <option value="">自动选择</option>
                  {caps?.pdf?.export_backends?.map((b) => (
                    <option key={b} value={b}>
                      {b}
                    </option>
                  ))}
                </Select>
              </div>
            )}
            <fieldset className="flex flex-wrap gap-5 items-center">
              <legend className="text-sm font-medium mb-2">版本</legend>
              <label className="text-sm flex gap-2">
                <input
                  type="radio"
                  name="edition"
                  checked={!bilingual}
                  onChange={() => setBilingual(false)}
                />
                单语版
              </label>
              <label className="text-sm flex gap-2">
                <input
                  type="radio"
                  name="edition"
                  checked={bilingual}
                  onChange={() => setBilingual(true)}
                />
                双语版
              </label>
              {bilingual && !subtitle && (
                <Select
                  aria-label="双语顺序"
                  value={order}
                  onChange={(e) =>
                    setOrder(e.target.value as "target_first" | "source_first")
                  }
                  className="max-w-40"
                >
                  <option value="target_first">译文在上</option>
                  <option value="source_first">原文在上</option>
                </Select>
              )}
            </fieldset>
            {!subtitle && (
              <label className="flex gap-2 items-center text-sm">
                <input
                  type="checkbox"
                  checked={about}
                  onChange={(e) => setAbout(e.target.checked)}
                />
                附加“关于此翻译”说明页
              </label>
            )}
            {!subtitle && bilingual && (
              <label className="flex gap-2 items-center text-sm">
                <input
                  type="checkbox"
                  checked={preserveStyle}
                  onChange={(e) => setPreserveStyle(e.target.checked)}
                />
                双语排版保留原文样式
              </label>
            )}
            <Button
              onClick={() => create.mutate()}
              disabled={create.isPending || !project?.fmt || !caps}
            >
              {create.isPending ? "提交中…" : "生成导出文件"}
            </Button>
            <p className="text-xs text-muted-foreground">
              导出文件名包含目标语言
              {project?.target_lang ? ` ${project.target_lang}` : ""}
              及双语标识。标点规范化仅作用于导出副本。
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b text-xs text-muted-foreground">
                <tr>
                  {["格式", "创建时间", "大小", "状态", "操作"].map((h) => (
                    <th key={h} className="text-left p-3">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {exports?.map((e) => (
                  <tr key={e.id} className="border-b last:border-0">
                    <td className="p-3">
                      {e.format.toUpperCase()}
                      <div className="text-xs text-muted-foreground mt-1">
                        {e.options?.bilingual ? "双语版" : "单语版"}
                      </div>
                    </td>
                    <td className="p-3">
                      {e.created_at
                        ? new Date(e.created_at).toLocaleString()
                        : "—"}
                    </td>
                    <td className="p-3">{formatBytes(e.size)}</td>
                    <td className="p-3">
                      <Badge
                        variant={
                          e.status === "done"
                            ? "success"
                            : e.status === "error"
                              ? "destructive"
                              : "secondary"
                        }
                      >
                        {e.status === "done"
                          ? "完成"
                          : e.status === "error"
                            ? e.error || "失败，请查看事件日志"
                            : "生成中"}
                      </Badge>
                    </td>
                    <td className="p-3">
                      {e.status === "done" && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={download.isPending}
                          onClick={() => download.mutate(e.id)}
                        >
                          下载
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
                {!exports?.length && (
                  <tr>
                    <td
                      colSpan={5}
                      className="p-8 text-center text-muted-foreground"
                    >
                      尚无导出文件。
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}
