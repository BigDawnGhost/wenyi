# 模块职责

[English](../architecture.md) · [翻译流程](pipeline.md) · [Web 开发](web.md)

本文描述已经实现的职责边界，替代已完成的重构设计稿。
下表 Core 路径相对于 `packages/core/wenyi_core/`。

| 领域 | 职责归属 |
|---|---|
| 入口 | `packages/cli/wenyi_cli/cli.py` 装配 CLI；`commands/` 通过调用上下文注册命令。Web API 和 Worker 位于 `apps/api/wenyi_api/`。 |
| 流程路由 | `pipeline/orchestrator.py` 装配服务、路由步骤并管理锁作用域，领域工作留在具体服务中。 |
| 翻译 | `pipeline/translation.py` 协调翻译；`translation_batch.py` 返回显式批次结果；`title_translation.py` 处理标题。 |
| 全书审校 | `pipeline/review_workflow.py` 协调会话；`review_checkpoint.py`、`review_rounds.py`、`review_chunks.py` 和 `review_results.py` 分别处理恢复、决策、执行和结果。 |
| 自动修复 | `pipeline/review_autofix.py` 协调独立发布服务；`autofix_candidates.py`、`autofix_plan.py` 和 `autofix_publish.py` 分离候选、可恢复索引与正式译文写回。 |
| 审校 Agent | `agents/review_*.py` 负责模型交互；`review/` 提供共享证据、类型和运行产物，不依赖 pipeline 编排。 |
| EPUB / HTML | `markup/` 负责共享的确定性锚点、注释、ruby 与段落标记处理；Reader 和 Writer 保留在 `ingest/` 与 `assemble/`。 |
| DOCX | `document_styles/docx.py` 负责纯样式策略；DOCX Reader 和 Writer 负责解析与文档生成。 |
| 持久化 | 领域服务使用 `storage/protocol.py`；`storage/file.py` 适配本地 RunStore/SQLite；Web 注入 `apps/api/wenyi_api/storage_pg.py` 保存 PostgreSQL 状态。 |

Core 不依赖 CLI 或 Web 框架；Agent 不依赖 pipeline 或具体状态存储。
共享标记处理和样式策略不依赖 LLM 或特定 Writer。SRT 保持独立轻量流程，
BabelDOC 保持外部 HTTP 服务。

初始化最后提交 manifest；发布先写可恢复索引，再更新正式译文。稳定段落身份、
一致导出快照与用量只合并一次等约束适用于所有存储后端。
完整约束与验证要求见[仓库指南](../../AGENTS.md)。
