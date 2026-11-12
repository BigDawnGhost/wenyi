# 架构与模块边界

本文说明当前模块的职责与依赖边界。用户可见的翻译行为见[流程指南](../guide/pipeline.md)，
状态、检查点和恢复规则见[流程开发契约](pipeline.md)。

## 依赖方向

```text
CLI / Web / Desktop
  → Orchestrator
    → Run / Prepare / Translate / Annotate / Review / Autofix / Finalize
      → agents / ingest / glossary / assemble / Storage ports
```

Core 不导入 CLI 或 HTTP 框架。Orchestrator 装配服务、路由步骤并管理锁作用域，
不直接调用领域函数，也不拥有线程池。Agent 不依赖 pipeline 编排或具体状态存储。
共享标记和样式策略不依赖 LLM 或特定 Writer。
BabelDOC 保持外部 HTTP 服务，不成为 Core 的 AGPL 依赖。

## 职责地图

Core 路径相对于 `packages/core/wenyi_core/`；应用与包路径相对于仓库根目录。

| 领域 | 职责归属 |
|---|---|
| 入口 | `packages/cli/wenyi_cli/cli.py` 装配 CLI；`commands/` 通过调用上下文注册命令。Web 入口位于 `apps/api/wenyi_api/`，本地 Desktop 入口位于 `apps/desktop/backend/wenyi_desktop/`。 |
| HTTP 应用 | `packages/backend/wenyi_backend/` 提供共享路由、schema、应用服务与平台端口。每个 app 和 worker 接收自身 backend context；平台适配器提供持久化、任务调度、凭据与进度遥测。 |
| Backend 执行 | `packages/backend/wenyi_backend/execution/` 分离类型化请求、工作流生命周期、领域操作与导出协调；`bindings.py` 显式注入项目/任务端口及现有平台服务，`wenyi_backend/workers/tasks.py` 保留稳定调度入口。 |
| 用户界面 | `packages/ui/` 提供共享页面、组件与界面翻译；`apps/web/` 和 `apps/desktop/frontend/` 提供独立入口与平台服务。只有 Desktop 包含原生导入/保存和凭据适配。 |
| Pipeline 目录结构 | `pipeline/` 按子阶段分组：`run/`（运行时、用量检查点、语言策略）、`prepare/`（原文解析、预扫、全书理解）、`translate/`（批次、术语、标题、上下文、精修）、`annotate/`（EPUB 注释、DOCX 样式）、`review/`、`autofix/` 与 `finalize/`。`pipeline/__init__.py` 转出全部模块，扁平与分子包两种 import 路径都可用。 |
| 流程路由 | `pipeline/orchestrator.py` 位于包顶层，装配服务、路由步骤并管理锁作用域，领域工作留在具体服务中。 |
| 原文准备 | `pipeline/prepare/input_preparation.py` 负责无模型解析、源文/配置身份与原文快照，由 CLI `parse`、Backend 预览和 `PreparationService` 共用。`packages/backend/wenyi_backend/source_view.py` 提供未初始化章节的只读原文投影；`pipeline/prepare/preparation.py` 协调初始化，并最后提交 manifest。 |
| 全书理解 | `pipeline/prepare/book_understanding.py` 拥有章节 digest、全书 synopsis、稳定顺序并发与可续跑缓存，显式接收 Agent/配置/用量，不接收整个 Runtime。 |
| 运行时与记账 | `pipeline/run/runtime.py` 装配 client/Agent 并绑定语言与计时；`pipeline/run/runtime_usage.py` 拥有唯一用量检查点，通过消费者所需的账本协议执行可恢复书籍/Review 发布。 |
| 翻译 | `pipeline/translate/translation.py` 协调翻译；`translation_batch.py` 返回显式批次结果；`title_translation.py` 处理标题。 |
| 全书审校 | `pipeline/review/review_workflow.py` 协调会话；`review_checkpoint.py`、`review_rounds.py`、`review_chunks.py` 和 `review_results.py` 分别处理恢复、决策、执行和结果。 |
| 自动修复 | `pipeline/autofix/review_autofix.py` 协调独立发布服务；`autofix_candidates.py`、`autofix_plan.py` 和 `autofix_publish.py` 分离候选、可恢复索引与正式译文写回。 |
| 审校 Agent | `agents/review_*.py` 负责模型交互；`review/` 提供共享证据、类型和运行产物，不依赖 pipeline 编排。 |
| EPUB / HTML | `markup/` 负责共享的确定性锚点、注释、ruby 与段落标记处理；Reader 和 Writer 保留在 `ingest/` 与 `assemble/`。`assemble/epub_package.py` 使用本地 XML 序列化和 `zipfile` 打包新生成的 EPUB 3，不依赖 EbookLib。 |
| DOCX | `document_styles/docx.py` 负责纯样式策略；DOCX Reader 和 Writer 负责解析与文档生成。 |
| 持久化 | 领域服务使用 `storage/protocol.py`；CLI 使用 `storage/file.py`，Desktop 为目录库中已登记项目库位置解析出的各项目目录注入 `storage/sqlite.py`，Web 注入 `apps/api/wenyi_api/storage_pg.py` 保存 PostgreSQL 状态。 |
| 设置重建 | `config_rebuild.py` 按当前 Core schema 校验已保存字段；`packages/backend/wenyi_backend/config_rebuild.py` 应用全局／项目作用域，各平台拥有备份与替换。 |
| CLI 文件迁移 | `storage/file_migrations.py` 拥有显式本地账本枚举、校验、原始字节备份、运行锁与原子发布；CLI 处理参数和展示，不发现 Web/Desktop 状态。 |

## 平台端口

Web 与 Desktop 共享行为，不共享项目状态或基础设施。Web 使用 PostgreSQL 与
Redis/Arq；Desktop 使用独立 SQLite 目录库、各项目 SQLite 状态及本地运行器，
不读取或迁移 CLI/Web 状态。共享 backend 不导入平台包，共享 UI 通过注入接收
原生能力而不是反向导入 app。Web Docker 镜像只安装 Web 与共享运行依赖；
Desktop sidecar 在独立环境构建，不包含 PostgreSQL、Redis 或 Arq 库。

每个 app、worker 和线程回调保留各自的 backend context。HTTP 路由及调度/装配绑定
解析作用域上下文；`routers/dependencies.py` 将请求绑定到显式服务输入。
执行、项目、任务准入和原文视图服务不通过隐式上下文或动态 DAL 转发寻找依赖。
准入与补偿是完整端口操作，事务由对应平台负责。

UI 宿主使用 `@wenyi/ui` 的显式导出，在挂载前配置平台服务。共享 schema 离线生成，
不启动生产服务。导入、路由、加载和跨宿主测试规则见[前端规范](frontend.md)，
schema 生成与传输契约见 [Web 开发](web.md)。

应用级诊断归 Desktop 所有，共享组件只提供可选观察钩子，不提供全局日志后端。
采集、隐私过滤和原生发布见 [Desktop 开发](desktop.md)，用户操作见
[Desktop 使用指南](../guide/desktop.md#诊断与反馈)。

## 存储边界

领域服务通过 `Storage` 与 `ArtifactStorage` 持久化，不直接打开本地 SQLite，
也不直接写文件状态。Artifact key 相对于运行目录，在所有平台使用 `/`，
按前缀查询时限制扫描范围。`storage/run_store.py` 拥有文件账本，不导入 pipeline 服务。
文件适配器显式暴露成员，不向账本透传任意属性。

`Storage` 的 `run_dir`、`source_dir` 和 `reviews_dir` 三个路径属性仍是兼容性缺口，
不代表允许增加领域层文件写入。新增功能优先使用基于身份的存储操作；
修改既有路径消费者时，需要一起更新适配器与契约测试。
不要假定所有适配器都支持协议之外的文件专用辅助方法。

每次操作的 Storage 由创建方在成功和失败路径中关闭。读取路由使用显式关闭作用域；
runner 每取得一个资源就登记清理，包括启动遥测时失败的情况。

### 已保存设置重建

重建与书籍状态、账本迁移分离，在 CLI 加载配置，以及 Web repository、
Desktop 目录库启动时运行。纯函数保留有效输入字段、丢弃无效项并校验合并后的
有效配置，不把旧名称转换成别名。不支持的默认模板和无效已存流程选择重置为当前默认值，
不在读取时继续解释。新设置写入仍使用严格校验。

CLI 获取按文件隔离的升级锁，重新核对原始字节，先原子发布按内容哈希命名的
完整备份，再原子替换 YAML。Web/Desktop 在同一个事务内向
`configuration_backups` 写入原文档和丢弃路径，再替换设置。
`project_id` 为空表示含 revision、默认模板元数据的全局设置，
非空则标识包含对应项目 `config` 和 `strategy` 的备份。有效文档不产生写入。
备份或替换失败会保留原文档；各项目事务独立，后续失败不回滚之前完成的重建。

Web 使用设置 advisory lock 与项目行锁；Desktop 使用短目录库写事务，
事务内不访问凭据、不请求模型、不执行文件系统操作。
两者均不修改项目状态、用量与冻结任务。
备份位置及用户可见的回退规则见[配置参考](../reference/configuration.md#saved-settings-reconstruction)。

## 执行与发布

工作流 runner 负责持久化任务复核、取消与资源清理，运行锁覆盖恢复和领域操作；
成功的目录库状态写入在实时统计作用域和运行锁退出后进行。异步取消先等待
同步检查点写入结束，再释放调度槽位。导出协调器在同一个导出锁内完成
claim、render、publish 和终态写入；渲染自身不发布文件。

暂停准入在一个目录库事务中检查最新非导出运行及活动状态。原文替换先提交预览缓存
失效，再发布新原文引用；缓存失效失败会回滚，目录库提交结果不确定时保留完整候选
文件，避免删除可能已发布的原文。这些边界不提供跨存储事务或自动孤立文件清理。

初始化最后提交 manifest；发布先写可恢复索引，再更新正式译文。稳定段落身份、
一致导出快照与用量只合并一次等约束适用于所有存储后端。
检查点、语言策略和提示词资源的详细规则见[流程开发文档](pipeline.md)。

### EPUB CPU worker

EPUB reader 和模板导出通过 `resource_pool.py` 按物理 XHTML 分配 CPU 工作：
至少 12 个资源且 XHTML 解压后总大小至少 2 MiB 时，启用最多 6 个 spawn worker，
并受可用 CPU 数量和亲和性限制。较小输入、daemon 进程和无法重新导入的交互入口
使用同一套函数串行执行。这些内部阈值独立于 LLM 并发，适用于 CLI、Web 和 Desktop。

每个父进程共用一个准入槽，避免多个 EPUB 操作叠加进程池；不同应用/worker 进程
仍拥有独立预算。每个池最多保留每个 worker 两个在途资源，按源文顺序产出结果，
退出前等待运行中的工作并取消剩余任务，再释放准入槽。worker 只接收显式数据，
不传递 Storage、数据库连接、凭据或平台上下文。

解析先标注资源，再按目标 XHTML 提取注释上下文，最后构建逻辑章节。
双语导出先汇总全书原文链接目标，再开始回填，每个资源只接收它实际引用的链接映射。
EPUB 语言和布局改写在资源任务内执行；ZIP 读写、包级协调与原子发布仍由父进程负责。
冻结打包的 CLI/Desktop 入口在导入应用前先分流 multiprocessing worker。

## 边界验证

- 依赖变更运行 `packages/core/tests/test_architecture_boundaries.py`；
  pipeline 装配变更同时运行 `test_orchestrator_contract.py`。
- 存储变更还需运行 `test_storage_injection.py` 和对应适配器测试。
- 共享 UI 变更验证两个宿主，在两端构建后运行 `pnpm test:frontend-boundaries`；
  schema 变更运行 `pnpm check:schema`。

完整安全约束见[仓库指南](../../../AGENTS.md)，测试命令见
[贡献指南](../CONTRIBUTING.md#python-回归测试)。
