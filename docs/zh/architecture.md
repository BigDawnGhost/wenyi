# 模块职责

[English](../architecture.md) · [翻译流程](pipeline.md) · [Web 开发](web.md)

本文描述已经实现的职责边界，替代已完成的重构设计稿。
下表 Core 路径相对于 `packages/core/wenyi_core/`。

| 领域 | 职责归属 |
|---|---|
| 入口 | `packages/cli/wenyi_cli/cli.py` 装配 CLI；`commands/` 通过调用上下文注册命令。Web 入口位于 `apps/api/wenyi_api/`，本地 Desktop 入口位于 `apps/desktop/backend/wenyi_desktop/`。 |
| HTTP 应用 | `packages/backend/wenyi_backend/` 提供共享路由、schema、应用服务与平台端口。每个 app 和 worker 接收自身 backend context；平台适配器提供持久化、任务调度、凭据与进度遥测。 |
| 用户界面 | `packages/ui/` 提供共享页面、组件与界面翻译；`apps/web/` 和 `apps/desktop/frontend/` 提供独立入口与平台服务。只有 Desktop 包含原生导入/保存和凭据适配。 |
| 流程路由 | `pipeline/orchestrator.py` 装配服务、路由步骤并管理锁作用域，领域工作留在具体服务中。 |
| 翻译 | `pipeline/translation.py` 协调翻译；`translation_batch.py` 返回显式批次结果；`title_translation.py` 处理标题。 |
| 全书审校 | `pipeline/review_workflow.py` 协调会话；`review_checkpoint.py`、`review_rounds.py`、`review_chunks.py` 和 `review_results.py` 分别处理恢复、决策、执行和结果。 |
| 自动修复 | `pipeline/review_autofix.py` 协调独立发布服务；`autofix_candidates.py`、`autofix_plan.py` 和 `autofix_publish.py` 分离候选、可恢复索引与正式译文写回。 |
| 审校 Agent | `agents/review_*.py` 负责模型交互；`review/` 提供共享证据、类型和运行产物，不依赖 pipeline 编排。 |
| EPUB / HTML | `markup/` 负责共享的确定性锚点、注释、ruby 与段落标记处理；Reader 和 Writer 保留在 `ingest/` 与 `assemble/`。 |
| DOCX | `document_styles/docx.py` 负责纯样式策略；DOCX Reader 和 Writer 负责解析与文档生成。 |
| 持久化 | 领域服务使用 `storage/protocol.py`；CLI 使用 `storage/file.py`，Desktop 在独立工作区注入 `storage/sqlite.py`，Web 注入 `apps/api/wenyi_api/storage_pg.py` 保存 PostgreSQL 状态。 |

Core 不依赖 CLI 或 HTTP 框架；Agent 不依赖 pipeline 或具体状态存储。
共享标记处理和样式策略不依赖 LLM 或特定 Writer。SRT 保持独立轻量流程，
BabelDOC 保持外部 HTTP 服务。

Web 与 Desktop 共享行为，不共享项目状态或基础设施。Web 使用 PostgreSQL 与
Redis/Arq；Desktop 使用独立 SQLite 目录库、各项目 SQLite 状态及本地运行器，
不读取或迁移 CLI/Web 状态。共享 backend 不导入平台包，共享 UI 通过注入接收
原生能力而不是反向导入 app。Web Docker 镜像只安装 Web 与共享运行依赖；
Desktop sidecar 在独立环境构建，不包含 PostgreSQL、Redis 或 Arq 库。

初始化最后提交 manifest；发布先写可恢复索引，再更新正式译文。稳定段落身份、
一致导出快照与用量只合并一次等约束适用于所有存储后端。
完整约束与验证要求见[仓库指南](../../AGENTS.md)。

`i18n/policy/` 提供纯类型化的语言操作注册表和确定性解析器。语言档案区分原文与目标角色，计划冻结实际使用的提示词资源和导出能力。Agent 消费提示词计划，导出适配器执行文本与排版操作，pipeline 服务通过 Storage 持久化阶段身份。CLI 与 Web 执行共享解析器，计划只通过只读的开发者 CLI 诊断展示。策略绑定与参数在源码中定义，不提供 YAML 开关。语义检查点与导出快照使用独立身份，排版调整不会使已付费翻译失效。首版实现与后续扩展见[可组合的语言策略与操作注入](design/language-policies.md)。
