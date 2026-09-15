export function ErrorNotice({ error }: { error?: unknown }) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
    >
      {error instanceof Error ? error.message : String(error)}
    </div>
  );
}

/** Preserve nested evidence, model usage and provider errors without truncating data. */
export function StructuredData({
  value,
  empty = "尚无记录",
  depth = 0,
}: {
  value: unknown;
  empty?: string;
  depth?: number;
}) {
  if (
    value === undefined ||
    value === null ||
    (Array.isArray(value) && !value.length)
  )
    return <p className="text-sm text-muted-foreground">{empty}</p>;
  if (typeof value !== "object")
    return (
      <span className="whitespace-pre-wrap break-words text-sm">
        {typeof value === "boolean" ? (value ? "是" : "否") : String(value)}
      </span>
    );
  if (Array.isArray(value))
    return (
      <div className="space-y-3">
        {value.map((item, i) => (
          <div key={i} className="rounded border p-3">
            <StructuredData value={item} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  return (
    <dl className="space-y-2 text-sm">
      {Object.entries(value as Record<string, unknown>).map(([key, item]) => (
        <div
          key={key}
          className={
            depth < 2
              ? "grid gap-1 sm:grid-cols-[minmax(100px,180px)_1fr]"
              : "space-y-1 border-l pl-3"
          }
        >
          <dt className="break-words text-muted-foreground">
            {LABELS[key] || key}
          </dt>
          <dd className="min-w-0">
            <StructuredData
              value={
                key === "status" && typeof item === "string"
                  ? STATUS_TEXT[item] || item
                  : item
              }
              empty="—"
              depth={depth + 1}
            />
          </dd>
        </div>
      ))}
    </dl>
  );
}

const LABELS: Record<string, string> = {
  usage: "模型用量",
  timing: "运行耗时",
  totals: "合计",
  calls: "调用次数",
  labels: "模型名称",
  by_model: "按模型",
  by_provider: "按提供商",
  by_stage: "按步骤",
  by_tier: "按档位",
  cache_hit_rate: "缓存命中率",
  cache_hit_tokens: "缓存命中 Token",
  cache_miss_tokens: "缓存未命中 Token",
  total_seconds: "总耗时（秒）",
  runs: "运行记录",
  schema_version: "记录版本",
  seconds: "耗时（秒）",
  profile: "模型配置",
  records: "发布记录",
  enabled: "已启用",
  issue_count: "审校问题数",
  patch_count: "修订补丁数",
  change_count: "建议变更数",
  clean_streak: "连续干净确认轮次",
  conflict_count: "冲突数",
  fix_round_count: "修订轮次",
  review_round_count: "审校轮次",
  blocked_issue_count: "受阻问题数",
  initial_issue_count: "首次审校问题数",
  fallback_agent_count: "回退核查次数",
  dismissed_issue_count: "已排除问题数",
  shadow_override_count: "影子修订数",
  unresolved_conflict_count: "未解决冲突数",
  autofix_failed_issue_count: "自动修复失败问题数",
  not_rereported_patch_count: "未再次报告的修订数",
  pre_arbitration_issue_count: "仲裁前问题数",
  arbitration_superseded_count: "仲裁替换数",
  autofix_applied_segment_count: "自动修复段落数",
  failed_issue_count: "修复失败问题数",
  failed_record_count: "失败发布记录",
  applied_change_count: "已应用变更数",
  applied_segment_count: "已修复段落数",
  applied_issue_fix_count: "已修复问题数",

  status: "状态",
  summary: "摘要",
  issues: "问题",
  changes: "建议变更",
  autofix: "自动修复",
  type: "类型",
  severity: "严重程度",
  detail: "说明",
  explanation: "说明",
  suggestion: "建议",
  source: "原文",
  target: "译文",
  target_before: "修订前",
  target_after: "修订后",
  before: "修订前",
  after: "修订后",
  chapter_index: "章节索引",
  segment_index: "段落索引",
  reason: "原因",
  evidence: "证据",
  error: "错误",
  model: "模型",
  provider: "提供商",
  operation: "操作",
  prompt_tokens: "输入 Token",
  completion_tokens: "输出 Token",
  total_tokens: "总 Token",
  cost: "费用",
  total_cost: "总费用",
  duration: "耗时",
  elapsed_seconds: "耗时（秒）",
  chapters_total: "总章节",
  chapters_done: "已完成章节",
  chapters_reviewed: "已审章节",
  terms: "术语",
  open_conflicts: "待裁决冲突",
  review_issues: "审校问题",
  empty_targets: "空译文",
  output: "输出",
  results: "结果",
  published: "已发布",
  failed: "失败",
  skipped: "已跳过",
  created_at: "创建时间",
};

const STATUS_TEXT: Record<string, string> = {
  completed: "已完成",
  done: "已完成",
  pending: "等待执行",
  running: "运行中",
  interrupted: "已中断",
  failed: "失败",
  error: "失败",
  paused: "已暂停",
  skipped: "已跳过",
};
