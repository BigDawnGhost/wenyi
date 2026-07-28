import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type EventOut } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const BADGE: Record<string, { variant: "info" | "success" | "warning" | "secondary"; label: string }> = {
  project: { variant: "info", label: "项目" },
  run_initialized: { variant: "secondary", label: "准备" },
  language_detected: { variant: "secondary", label: "准备" },
  analysis_saved: { variant: "secondary", label: "准备" },
  book_synopsis_saved: { variant: "secondary", label: "准备" },
  batch_translated: { variant: "info", label: "翻译" },
  batch_skipped: { variant: "secondary", label: "翻译" },
  chapter_done: { variant: "success", label: "完成" },
  chapter_reviewed: { variant: "warning", label: "审校" },
  batch_glossary_extracted: { variant: "secondary", label: "术语" },
  assembled: { variant: "success", label: "导出" },
};

function badgeFor(type: string) {
  return BADGE[type] || { variant: "secondary" as const, label: type.split("_")[0] || "事件" };
}

function describe(e: EventOut): string {
  const p = e.payload;
  switch (e.type) {
    case "language_detected": return `检测到源语言：${p.source_lang}`;
    case "analysis_saved": return "风格分析完成";
    case "book_synopsis_saved": return "生成全书概览";
    case "batch_translated": return `第 ${(Number(p.chapter) ?? 0) + 1} 章 批次完成（${p.count} 段）`;
    case "batch_skipped": return `第 ${(Number(p.chapter) ?? 0) + 1} 章 批次跳过（已译）`;
    case "chapter_done": return `第 ${(Number(p.chapter) ?? 0) + 1} 章《${p.title}》翻译完成`;
    case "chapter_reviewed": return `第 ${(Number(p.chapter) ?? 0) + 1} 章审校：${p.issue_count} 个问题`;
    case "assembled": return `导出文件：${(p.outputs as string[])?.join(", ") || ""}`;
    case "run_initialized": return `项目初始化：${p.chapters} 章`;
    default: return e.type;
  }
}

export default function EventsPage() {
  const { pid = "" } = useParams();
  const { data: events } = useQuery({ queryKey: ["events", pid], queryFn: () => api.listEvents(pid), enabled: !!pid, refetchInterval: 5000 });

  return (
    <>
      <PageHeader title="事件日志" subtitle="项目生命周期中的关键事件（每 5 秒刷新）" />
      <PageContainer>
        <Card>
          <CardContent className="p-4">
            {!events?.length ? (
              <p className="text-sm text-muted-foreground text-center py-8">暂无事件。</p>
            ) : (
              <div className="space-y-2">
                {events.map((e) => {
                  const b = badgeFor(e.type);
                  return (
                    <div key={e.id} className="flex items-start gap-3 text-sm py-1.5">
                      <span className="text-xs text-muted-foreground whitespace-nowrap mt-0.5">{e.created_at ? new Date(e.created_at).toLocaleString() : ""}</span>
                      <Badge variant={b.variant}>{b.label}</Badge>
                      <span>{describe(e)}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}
