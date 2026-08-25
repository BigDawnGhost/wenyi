"""流水线：编排 façade、共享运行时与各领域服务。

模块职责（依赖方向固定，任何下层模块不得反向导入 orchestrator.py）：

  orchestrator.py     纯流程控制 façade：步骤路由、阶段顺序、锁作用域、异常短路。
  runtime.py          共享运行时：唯一构造并持有 Config/LLMClient/Agent，统一
                      语言恢复、event sink、usage checkpoint/flush、metrics session、
                      阶段计时、状态快照与源文件哈希验证。
  preparation.py      准备服务：状态定位、输入解析、语言检测、初始化事务、风格
                      分析、初始术语、RollingContext、全书理解预扫。
  annotations.py      注释服务：注释上下文映射、续段重组、串行定位与失败降级。
  translation.py      翻译服务：批次续跑、章/批翻译、润色、术语快照与抽取、
                      标题与目录翻译。
  review_workflow.py  Review 服务：并行审校、证据 Agent Loop、冲突仲裁、Fixer、
                      影子译文与盲复审状态机。
  finalization.py     ReportService（术语库生命周期、报告）与 AssemblyService
                      （实时状态导出 / 只读快照导出）。
  language.py         与具体阶段无关的语言规范化纯函数。
  runstore.py         运行态持久化：manifest/chapters/usage/events/reviews 等。
  context.py          滚动上下文。
  metrics.py          单次运行账本。
  checks.py           结果一致性检查。

Review 证据索引与运行目录位于顶层 ``trans_novel.review``。
字幕翻译位于顶层 ``trans_novel.srt``（不走本包编排）。
"""
