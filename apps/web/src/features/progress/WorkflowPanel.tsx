import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ProgressMessage } from "@/lib/ws";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/data";

const TASKS: Record<string, string> = { parse: "原文解析", prepare: "译前准备", translation: "全书翻译", chapter_translation: "单章翻译", review: "全书审校", srt: "字幕翻译", model_compare: "模型对比" };
const STATES: Record<string, string> = { not_started: "尚未开始", queued: "排队中", running: "运行中", paused: "已暂停", error: "失败", interrupted: "已中断", done: "已完成" };
export function WorkflowPanel({ pid, msg }: { pid: string; msg: ProgressMessage | null }) {
  const query = useQuery({ queryKey: ["workflow", pid], queryFn: () => api.getWorkflow(pid), refetchInterval: 2500 });
  const workflow = query.data;
  const live = msg?.run_id && msg.run_id === workflow?.run_id && msg.label ? msg : workflow?.progress;
  return <Card><CardContent className="p-5 space-y-4">
    <div><h2 className="font-medium">当前翻译流程</h2><p className="text-xs text-muted-foreground mt-1">{workflow?.source === "snapshot" ? "显示最近一次任务提交时的流程。修改项目配置会在下次启动或恢复任务时生效。" : "显示当前项目配置，尚未提交任务。"}</p></div>
    <ErrorNotice error={query.error} />
    {workflow && <>
      <div className="flex flex-wrap gap-3 text-sm"><strong>{TASKS[workflow.kind] || workflow.kind}</strong><span>{STATES[workflow.status] || workflow.status}</span></div>
      <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{workflow.stages.map((stage, index) => <li key={stage.id} className={`rounded-lg border p-3 ${stage.enabled ? "bg-accent/30" : "opacity-50"}`}><div className="text-xs text-muted-foreground">{index + 1} · {stage.enabled ? "已启用" : "已关闭"}</div><div className="text-sm font-medium mt-1">{stage.label}</div></li>)}</ol>
      <div role="status" className="rounded border p-3 text-sm"><span className="text-muted-foreground">最近步骤：</span>{live?.label ? String(live.label) : "等待后台上报进度"}{live && Number(live.total) > 0 && <span className="ml-2">（{Number(live.done) || 0}/{Number(live.total)}）</span>}</div>
      <p className="text-xs text-muted-foreground">润色随翻译批次执行；以上卡片表示流程配置，完成情况以任务状态及最近步骤为准。导出任务在导出页单独查看。</p>
    </>}
  </CardContent></Card>;
}
