# 模块职责

[English](../architecture.md) · [翻译流程](pipeline.md) · [Web 开发](web.md)

本文描述已经实现的模块职责与边界。
下表 Core 路径相对于 `packages/core/wenyi_core/`。

[功能组件解耦记录](design/component-decoupling.md)说明已完成的 F1–F7 拆分、
保持不变的契约及验证限制。

仅面向 Desktop 的应用级日志与用户主动导出诊断包见
[使用说明](desktop.md#诊断与反馈)和[增量诊断设计](design/application-diagnostics.md)。
采集和原生发布归 Desktop 所有，共享组件仅提供可选观察钩子。

| 领域 | 职责归属 |
|---|---|
| 入口 | `packages/cli/wenyi_cli/cli.py` 装配 CLI；`commands/` 通过调用上下文注册命令。Web 入口位于 `apps/api/wenyi_api/`，本地 Desktop 入口位于 `apps/desktop/backend/wenyi_desktop/`。 |
| HTTP 应用 | `packages/backend/wenyi_backend/` 提供共享路由、schema、应用服务与平台端口。每个 app 和 worker 接收自身 backend context；平台适配器提供持久化、任务调度、凭据与进度遥测。 |
| Backend 执行 | `packages/backend/wenyi_backend/execution/` 分离类型化请求、工作流生命周期、领域操作与导出协调；`bindings.py` 显式注入项目/任务端口及现有平台服务，`workers/tasks.py` 保留稳定调度入口。 |
| 用户界面 | `packages/ui/` 提供共享页面、组件与界面翻译；`apps/web/` 和 `apps/desktop/frontend/` 提供独立入口与平台服务。只有 Desktop 包含原生导入/保存和凭据适配。 |
| 流程路由 | `pipeline/orchestrator.py` 装配服务、路由步骤并管理锁作用域，领域工作留在具体服务中。 |
| 原文准备 | `pipeline/input_preparation.py` 负责无模型解析、源文/配置身份与原文快照，由 CLI `parse`、Backend 预览和 `PreparationService` 共用。`packages/backend/wenyi_backend/source_view.py` 提供未初始化章节的只读原文投影；`pipeline/preparation.py` 协调初始化，并最后提交 manifest。 |
| 全书理解 | `pipeline/book_understanding.py` 拥有章节 digest、全书 synopsis、稳定顺序并发与可续跑缓存，显式接收 Agent/配置/用量，不接收整个 Runtime。 |
| 运行时与记账 | `pipeline/runtime.py` 装配 client/Agent 并绑定语言与计时；`pipeline/runtime_usage.py` 拥有唯一用量检查点，通过消费者所需的账本协议执行可恢复书籍/Review 发布。 |
| 翻译 | `pipeline/translation.py` 协调翻译；`translation_batch.py` 返回显式批次结果；`title_translation.py` 处理标题。 |
| 全书审校 | `pipeline/review_workflow.py` 协调会话；`review_checkpoint.py`、`review_rounds.py`、`review_chunks.py` 和 `review_results.py` 分别处理恢复、决策、执行和结果。 |
| 自动修复 | `pipeline/review_autofix.py` 协调独立发布服务；`autofix_candidates.py`、`autofix_plan.py` 和 `autofix_publish.py` 分离候选、可恢复索引与正式译文写回。 |
| 审校 Agent | `agents/review_*.py` 负责模型交互；`review/` 提供共享证据、类型和运行产物，不依赖 pipeline 编排。 |
| EPUB / HTML | `markup/` 负责共享的确定性锚点、注释、ruby 与段落标记处理；Reader 和 Writer 保留在 `ingest/` 与 `assemble/`。 |
| DOCX | `document_styles/docx.py` 负责纯样式策略；DOCX Reader 和 Writer 负责解析与文档生成。 |
| 持久化 | 领域服务使用 `storage/protocol.py`；CLI 使用 `storage/file.py`，Desktop 在独立工作区注入 `storage/sqlite.py`，Web 注入 `apps/api/wenyi_api/storage_pg.py` 保存 PostgreSQL 状态。 |
| CLI 文件迁移 | `storage/file_migrations.py` 拥有显式本地账本枚举、校验、原始字节备份、运行锁与原子发布；CLI 处理参数和展示，不发现 Web/Desktop 状态。 |

Core 不依赖 CLI 或 HTTP 框架；Agent 不依赖 pipeline 或具体状态存储。
共享标记处理和样式策略不依赖 LLM 或特定 Writer。SRT 保持独立轻量流程，
BabelDOC 保持外部 HTTP 服务。

Web 与 Desktop 共享行为，不共享项目状态或基础设施。Web 使用 PostgreSQL 与
Redis/Arq；Desktop 使用独立 SQLite 目录库、各项目 SQLite 状态及本地运行器，
不读取或迁移 CLI/Web 状态。共享 backend 不导入平台包，共享 UI 通过注入接收
原生能力而不是反向导入 app。Web Docker 镜像只安装 Web 与共享运行依赖；
Desktop sidecar 在独立环境构建，不包含 PostgreSQL、Redis 或 Arq 库。

UI 宿主只使用 `@wenyi/ui` 的显式包导出：app、platform、API/类型、HTTP 辅助、
i18n、文档语言、样式、允许复用的基础组件、设置布局与格式化。宿主 `@/` 别名
只指向自身源码，共享 UI 使用相对导入。`api/types.ts` 依赖生成的 schema，
不依赖 API client；`platform/contracts.ts` 组合传输、进度、偏好、活动和可选能力。
`api/client.ts` 与 React 挂载前配置的 `platform()` 门面仍各自保持唯一实现；
旧 `lib/api.ts` 实现/类型转发已删除。

`scripts/export_openapi.py` 使用不可调用的运行端口装配共享 HTTP schema，不导入平台
入口，也不启动生产 lifespan。CI 通过 `pnpm check:schema` 比较离线生成类型与已提交
契约。前端边界测试检查宿主导入解析，从路由清单提取懒加载分包，并保留独立 URL
断言。全部 20 个跨宿主套件、API fixture 和 select 测试支持位于
`packages/ui/tests/e2e/`，不进入运行时导出。宿主用自身 bootstrap 与能力选项显式
注册；禁止跨宿主测试导入和 fixture 转发。

Backend 执行、项目、任务准入和原文视图服务显式接收依赖，不通过隐式上下文或动态
DAL 转发寻找依赖。HTTP 路由及调度/装配绑定负责解析作用域上下文，
`routers/dependencies.py` 将 HTTP 请求绑定到服务输入。共享动态 DAL 已删除，
PostgreSQL 适配器显式列出支持的方法；平台准入与补偿仍是完整端口操作，
保留各自原有的事务边界。
工作流 runner 负责持久化任务复核、取消与资源清理，运行锁覆盖恢复和领域操作；
成功的目录库状态写入在实时统计作用域和运行锁退出后进行。异步取消先等待
同步检查点写入结束，再释放调度槽位。导出协调器在同一个导出锁内完成
claim、render、publish 和终态写入；渲染自身不发布文件。

每次操作的 Storage 由创建方在成功与失败路径中显式关闭。读取路由使用关闭作用域，
包含异常路径；runner 每取得一个资源就登记清理，遥测启动失败也不泄漏已取得的资源。
暂停准入在一个目录库事务中检查最新非导出运行及活动状态。原文替换先提交预览缓存
失效，再发布新原文引用；缓存失效失败会回滚，目录库提交结果不确定时保留完整候选
文件，避免删除可能已发布的原文。这些安全修复不引入跨存储事务，也不自动清理孤立文件。

初始化最后提交 manifest；发布先写可恢复索引，再更新正式译文。稳定段落身份、
一致导出快照与用量只合并一次等约束适用于所有存储后端。
完整约束与验证要求见[仓库指南](../../AGENTS.md)。

`i18n/policy/` 提供纯类型化的语言操作注册表和确定性解析器。语言档案区分原文与目标角色，计划冻结实际使用的提示词资源和导出能力。Agent 消费提示词计划，导出适配器执行文本与排版操作，pipeline 服务通过 Storage 持久化阶段身份。CLI 与 Web 执行共享解析器，计划只通过只读的开发者 CLI 诊断展示。策略绑定与参数在源码中定义，不提供 YAML 开关。语义检查点与导出快照使用独立身份，排版调整不会使已付费翻译失效。当前行为与开发者诊断见[内置语言策略](configuration.md#内置语言策略)。
