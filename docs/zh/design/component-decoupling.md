# 功能组件解耦与目录规划

[English](../../design/component-decoupling.md) · [已实现的架构](../architecture.md)

## 状态与结论

**渐进实施方案。** 初次检查基线为 `80d448f`，初稿仅增加设计文档。F3 中的无模型输入解析现已落地，包含 CLI `parse` 和 Web/Desktop 只读原文展示，见[当前模块职责](../architecture.md)与 [CLI 流程](../cli.md#常用命令)。其余目标路径仍为规划，不代表已经完成迁移。

保留现有模块化单仓库与部署边界，先在**包内部**解耦职责，再整理目录。每一步迁移保留公开入口，只有调用方和测试全部迁移后才删除兼容转发。

| 方案 | 取舍 | 结论 |
|---|---|---|
| 只搬目录、拆小文件 | 文件看起来更短，但隐式依赖与重复规则仍在 | 不作为主要任务 |
| 按用例和消费者逐步抽取接口 | 改动易审查；兼容转发必须有明确删除条件 | **推荐** |
| 重建领域包或拆微服务 | 引入大量导入、打包与运维变化，当前没有对应部署需求 | 不纳入本次范围 |

本方案不要求迁移数据库或状态格式，不增加运行服务或 workspace 包，不修改提示词，也不重新设计界面。

## 1. 已经合理、应继续保留的边界

- Core 拥有翻译行为，CLI、共享 Backend 与平台适配依赖 Core，不反向依赖。
- [Orchestrator](../../../packages/core/wenyi_core/pipeline/orchestrator.py#L17) 已经将领域工作委派给服务。阶段路由和锁作用域是其合理职责，不应为了“更薄”继续拆散。
- [BackendContext](../../../packages/backend/wenyi_backend/context.py#L143) 按应用注入适配器；ContextVar 属于请求/任务作用域，**不是**进程级 Web/Desktop 开关。线程与回调中的上下文传播必须保留。
- Web 拥有 PostgreSQL 与 Redis/Arq；Desktop 拥有独立工作区、SQLite 目录库、凭据与本地运行器。PostgreSQL 适配器必须留在 `apps/api`，不能移入 Core。
- 共享 UI 已有 feature 目录与宿主能力注入。继续区分路由、外壳、基础组件与功能组件，不为更换父目录名称搬动所有页面。
- `ArtifactStorage`、Review 证据/模型、markup、DOCX 样式策略、语言策略及独立 SRT 执行已经形成有效边界，应复用而不是再造一套抽象。
- 已有 [Python 边界测试](../../../packages/backend/tests/test_backend_boundaries.py#L11)、[前端边界测试](../../../packages/ui/tests/platform-boundaries.test.mjs#L31) 和[干净安装隔离检查](../../../scripts/check_backend_packages.py#L19)。需要定向补强，而不是推倒重建。

## 2. 检查发现与建议拆分点

优先级表示实施顺序，不表示已确认的线上故障。文件长、存在装配对象或启动 singleton，本身都不构成缺陷。

### F1：任务入口与业务执行共用一个复杂生命周期

**证据：** [workers/tasks.py](../../../packages/backend/wenyi_backend/workers/tasks.py#L50) 绑定 worker context；其中 [_execute](../../../packages/backend/wenyi_backend/workers/tasks.py#L196) 同时负责配置解析、持久化任务身份校验、取消监控、领域操作和遥测/存储清理。[Desktop LocalRuntime](../../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L258) 也通过相同的 worker 字典形状调用这些函数。

**风险：** 修改调度调用或任务终态处理，会触碰业务执行链。迟到回调与重复投递的安全性依赖嵌在链路内的任务身份及锁检查。

**P1 拆分：** 引入类型明确的执行请求与显式执行依赖。一个 runner 统一拥有执行前身份复核、中断、清理和终态处理；具体操作函数只负责预览、书籍或字幕用例。`workers/tasks.py` 保留原任务名称及签名，变成薄兼容入口。Web Arq 与 Desktop 调度继续属于平台适配；CLI 仍直接调用 Core，不接入 HTTP 应用层。

runner 从持久任务身份复核、恢复、执行直到成功状态写入，始终拥有书级长运行锁。导出执行单独抽取：同一个导出协调器在一个 `export_lock` 内完成 claim、render、publish 与终态写入。书籍渲染使用 Core 一致快照和 assemble lock；SRT 保留独立的 state-lock 快照路径。渲染不更新目录库状态，发布不调用 Writer。复用现有导出计划、路径和响应辅助模块，不再创建第二套导出策略。

### F2：作用域上下文正确，但动态转发掩盖服务的真实依赖

**证据：** [dal.__getattr__](../../../packages/backend/wenyi_backend/dal.py#L1) 动态转发到当前 repository；[Repository](../../../packages/backend/wenyi_backend/context.py#L28) 聚合项目、任务与导出操作；[PostgresRepository](../../../apps/api/wenyi_api/adapters.py#L21) 又转发到平台 DAL。执行函数签名无法呈现这些依赖。

**风险：** 服务能够在不修改输入契约的情况下获取更多持久化能力，动态转发也削弱了静态检查。

**P1 拆分，与 F1 配套：** 根据抽出的消费者定义任务和项目协议，显式传入所需端口。`BackendContext` 继续作为装配入口，已有 settings/export/telemetry 契约继续复用。逐步用有类型的调用代替动态转发。

**不能**把逻辑上耦合的准入与补偿拆成无关仓储调用。保留各平台现有边界：Desktop 使用单个 SQLite 事务；Web 使用现有锁、顺序写入及失败补偿，并不是同一个数据库事务。增强 Web 原子性属于单独的行为改动。现有 `PostgresRepository`、`LocalBackend` 可以同时实现多个小协议，不必每个方法创建一个新对象，也不能把 SQL 移进共享 Backend。

### F3：输入准备同时承担解析、初始化和模型辅助的全书理解

**证据：** [PreparationService](../../../packages/core/wenyi_core/pipeline/preparation.py#L63) 包含解析缓存校验、状态定位、初始化与全书理解。Backend 的[预览路径](../../../packages/backend/wenyi_backend/workers/tasks.py#L72) 调用其静态缓存辅助方法，却自行组装解析参数、源文检查和解析缓存内容；Core 的 [prepare 路径](../../../packages/core/wenyi_core/pipeline/preparation.py#L149) 还有独立解析分支。

**风险：** 解析参数、缓存身份和源文变更处理有多个需要同步维护的调用方；生成预览不应依赖一个已经装配模型的 runtime。

**P1 拆分：** 提取输入准备 API，集中解析参数、源文身份检查与已验证缓存文档，返回文档及其身份；支持缓存时通过注入的产物接口访问。Backend 保留 HTTP 预览数据投影，Core 保留初始化及 manifest 提交职责。

第一步保持每个调用方现有缓存读写策略，不顺手新增缓存或改变失效规则。PDF 在转换前已知状态目录并写初始化标记，其他格式按书名定位，这个差别不能被“统一”抹去。之后再提取全书理解及其稳定顺序并发、检查点，`PreparationService` 继续协调阶段。

### F4：运行时装配同时拥有可恢复的用量发布

**证据：** [PipelineRuntime](../../../packages/core/wenyi_core/pipeline/runtime.py#L36) 构造 client 和 Agent；[flush_usage](../../../packages/core/wenyi_core/pipeline/runtime.py#L113) 则更新 client 检查点并同时发布书籍和 Review 账本。[Storage](../../../packages/core/wenyi_core/storage/protocol.py#L30) 是较宽的聚合接口，但已经存在仅访问产物的端口。

**风险：** 看似简单的账本代码搬移可能改变检查点时机、恢复或只计费一次语义。如果拆出的服务继续接收整个 runtime，耦合并没有消失。

**P2 拆分：** 先提取按运行持有的用量协调器，显式依赖 client 用量与账本接口。每次运行仍只有一个 client 和一个检查点所有者，复用现有用量计算函数。保留聚合 `Storage` 兼容接口，仅为真实消费者引入窄协议。

不要预建大量空端口。语言绑定和计时暂留 Runtime，等有实际消费者需要独立契约时再拆。此阶段不搬 `RunStore`，不改存储 schema。

### F5：UI 契约暴露实现路径，并存在仅类型层面的反向依赖

**证据：** [platform.ts](../../../packages/ui/src/platform.ts#L1) 从 API 实现导入 `ProjectDetail`，而 [lib/api.ts](../../../packages/ui/src/lib/api.ts#L1) 又导入平台访问函数和源文件能力。这是**类型反向依赖**，不能据此认定运行时循环故障。包的[通配导出](../../../packages/ui/package.json#L6)、宿主 [TypeScript 别名](../../../apps/desktop/frontend/tsconfig.json#L16) 和 [Vite 别名](../../../apps/desktop/frontend/vite.config.ts#L7) 暴露共享 UI 内部路径；[DesktopCredential](../../../apps/desktop/frontend/src/DesktopCredential.tsx#L6) 实际消费了基础组件的实现路径。

**风险：** 移动私有 UI 模块会连带修改宿主，基础契约又依赖其服务的 client 实现。这是公共 API 边界问题，不是“宿主依赖共享 UI”这个方向有错。

**P1 拆分，可独立并行：** 先提取从 schema 推导的 API 类型，让平台契约与 client 都依赖类型模块。保留唯一 API client。将传输/进度、偏好/活动和可选原生 UI 能力组合成较小接口，保留 React 挂载前的宿主配置及 `platform()` 兼容入口。

为 App、平台契约、HTTP 辅助、i18n、样式和允许复用的基础组件设置显式公共入口。package exports、TypeScript 与 Vite 解析必须同步修改，不能继续用通配 alias 绕过导出限制。宿主 `@/` 转向自身 `src` **之前**，还须转换共享 UI 内部的 `@/` 导入或提供包内解析策略。

查询和 mutation 仍由对应 feature 拥有。按真实消费者逐步拆分 endpoint 模块，不新增第二个状态缓存、通用 service 框架或独立 API-client 包。

### F6：部分共享验证仍归属于某个宿主，或重复维护契约

**证据：** Desktop 的 [fixtures](../../../apps/desktop/frontend/tests/fixtures.ts#L1) 和[外壳测试](../../../apps/desktop/frontend/tests/unified-shell.spec.ts#L1) 导入 Web 测试；[产物边界测试](../../../packages/ui/tests/platform-boundaries.test.mjs#L14) 在已有运行时 route manifest 之外手写 chunk 名称清单。[Schema 生成](../../../package.json#L15) 依赖运行中的 localhost API；本次查阅的 [CI](../../../.github/workflows/tests.yml#L94) 没有重新生成并比较契约。

**风险：** 共享测试改动意外地由 Web 持有；新增路由或修改 schema 时，重复的测试/类型输入可能未同步。这些是护栏缺口，不表示当前类型或路由已经出错。

**P0/P1 拆分：** 使用共享 app factory、fake ports 且不启动生产 lifespan，离线导出确定性 OpenAPI；生成 TypeScript 到临时文件，与已提交 schema 比较，继续维持单一来源。路由枚举来自 manifest，但必须保留对必要 URL、重定向、audience 筛选和外壳连续性的独立断言，避免“从实现生成预期”导致测试自证。

把无平台的 fixture 与参数化共享行为移到 UI 测试支持目录，各宿主用自己的 setup 显式注册。原生保存/拖入/凭据/bootstrap 与浏览器行为继续分开验证。测试支持不得进入运行时导出或生产 bundle。

### F7：CLI 迁移命令直接调用文件存储私有方法

**证据：** [migrate_usage](../../../packages/cli/wenyi_cli/model_commands.py#L114) 了解账本路径、自行创建备份，并调用 `RunStore._write_json`。

**风险：** 存储内部重构可能破坏用户可见的迁移命令。

**P2 拆分：** 提供公开的文件存储迁移函数，拥有枚举、备份、锁和原子写入，复用纯账本转换函数；CLI 只负责参数校验和展示。这是特定的 CLI 文件状态操作，不新增 Web/Desktop 迁移路由。

工作流命令在后续修改时可以提取纯参数校验，但不要求先重排所有 CLI 命令。

## 3. 目标目录

下图是**选定模块的目标结构**，不是完整目录树。`+` 表示规划新增，`~` 表示保留路径但收窄职责；未列出的模块不动。只有某阶段真正提供实现时才创建对应模块。

```text
packages/
  core/wenyi_core/
    pipeline/
      orchestrator.py                  # unchanged facade
      preparation.py                  # ~ initialization and stage coordination
      input_preparation.py            # extracted parsing/cache rules shared with preview
      book_understanding.py           # + digest/synopsis coordination, later stage
      runtime.py                      # ~ composition, language binding and timing
      runtime_usage.py                # + run-owned usage checkpoint/publication
      runstore.py                     # unchanged file-state compatibility layer
    storage/
      protocol.py                     # retained; consumer-sized protocols as needed
      file.py / sqlite.py             # retained adapters
      file_migrations.py              # + public CLI file migration operation
  backend/wenyi_backend/
    application.py / context.py       # retained HTTP and adapter composition
    ports/
      projects.py / jobs.py           # + consumer-sized persistence contracts
    execution/
      contracts.py                    # + typed run request and dependencies
      runner.py                       # + identity, cancellation, cleanup, terminal state
      operations.py                   # + preview/book/SRT dispatch into Core
      exporting.py                    # + rendering/publication with distinct functions
    workers/tasks.py                  # ~ stable task entry-point wrappers
    routers/                          # retained HTTP translation layer
    export_plan.py / export_paths.py  # retained, not duplicated
    dal.py                           # transitional forwarding, no new consumers
  ui/
    src/
      App.tsx / routes/              # retained shell and routing
      platform.ts                    # ~ compatibility entry point
      platform/contracts.ts          # + composable host capability contracts
      platform/runtime.ts            # + pre-mount configuration and access
      api/types.ts                   # + aliases derived from generated schema
      api/client.ts                  # + the existing single client, relocated later
      lib/api.ts                     # ~ temporary API re-export
      components/ui/index.ts         # + approved host-facing primitives
      features/                      # retained; feature-local queries/components
      i18n/                          # retained
    tests/e2e/
      fixtures.ts / contracts.ts     # + host-neutral support, not runtime exports
  shared-schema/src/api.d.ts          # retained generated contract
apps/
  api/wenyi_api/                      # PostgreSQL, Redis/Arq and Web assembly stay here
  web/src/                           # browser host stays here
  desktop/
    backend/wenyi_desktop/            # catalog, local runner, credentials stay here
    frontend/src/                    # native host and optional UI stay here
    src/                             # Rust IPC/security boundary stays here
```

抽取后的逻辑依赖方向：

```text
CLI ----------------------------------------> Core facade/services
Web Arq / Desktop runner -> Backend execution -> Core facade/services
HTTP routes -------------> Backend use cases -> explicit platform ports
Web / Desktop UI hosts --> shared UI public entry points
UI features -> API client -> platform contracts -> generated API types
Core services -> Storage/ArtifactStorage ports <- file / SQLite / PostgreSQL adapters
```

装配层可以知道具体适配器；业务服务不能通过导入平台包来发现适配器。不新增泛化 `utils/`、全局服务定位器、`common` 包或通用事件总线。

## 4. 实施阶段与验收标准

阶段表示可审查的工作单元，不是工期承诺。目录搬迁、行为改变与持久化迁移不能混进同一次改动。

| 阶段 | 范围与依赖 | 必须提供的验收证据 |
|---|---|---|
| S0：补护栏 | 固定基线；为目标边界增加 schema 漂移、解析后导入与路由检查 | 扫描非空；人为构造的越界导入能被拦截；fake-port schema 导出不接触外部服务；两端构建先于 bundle 检查 |
| S1：UI 公共边界 | F5 类型与 exports，再处理 F6 测试共享；S0 后可独立于 Python 实施 | 两端 typecheck/build、边界测试和受影响 E2E 通过；URL、query key、DOM/外壳、原生能力与凭据行为不变 |
| S2：后端执行 | F1 runner 与其实际需要的 F2 端口；先保留 wrapper，再迁移宿主调用 | 覆盖重复/过期任务、暂停取消、清理补偿、锁竞争、重复导出及发布/终态失败；并发 app/worker 隔离和 `to_thread`/原始线程/回调传播；隔离 PG/Redis 与 Desktop runner 契约 |
| S3：共享输入准备 | F3；S0 后可开始，与 S2 协调预览调用点 | 缓存命中/失效、配置/源文不匹配、PDF 失败重试、manifest 最后提交及无模型预览测试；全书理解另行抽取 |
| S4：账本与文件迁移 | 消费方接口稳定后分别处理 F4/F7，不合为一个改动 | 崩溃/重试证明书籍与 Review 用量只合并一次；备份和原子写入不变；完整 Python 与存储后端验证 |
| S5：移除兼容转发 | 所有调用方迁移、打包检查通过后再做 | 废弃入口无剩余调用；文档、测试和扫描器一起更新；安装检查确实发现实际模块和资源 |

每次抽取先用测试固定现有行为。真正的缺陷修复应先有失败回归用例，并与行为不变的搬迁分开。某阶段若无法保留契约，应暂停该阶段、调整设计，而不是把变了的测试快照当作新标准。

**回退策略：** 迁移期由旧公开入口委派新实现，不维护两套独立演进的逻辑。本方案不改变持久化格式，抽取失败可以回退代码，无需回滚数据。不得通过删除用户状态、缓存、书籍或导出产物来撤销实验。

## 5. 不可改变的契约

1. 保留源文 hash、段落/章节身份、注释锚点、DOCX 样式与 `babeldoc_id`；`None` 与有意保存的空译文继续区分。
2. 初始化最后提交 manifest；保留已完成批次跳过、Review 检查点恢复，以及并发结果按原始顺序合并。
3. 保留运行/状态/事件/导出锁作用域、一致快照与文件原子发布；慢原生操作或外部调用不得持有目录库写事务。
4. 用量可恢复发布、Review 增量只合并一次；事件项目/运行身份、持久化任务名称和状态、过期任务拒绝与配置快照不变。
5. Review 只编辑影子译文；显式 Autofix 仍先写可恢复索引，再更新正式译文。
6. SRT 不接入书籍 Orchestrator、术语库或全书 Review。BabelDOC 继续是外部 AGPL HTTP 服务，不成为 Core 依赖。
7. 保留 Web/Desktop 状态与依赖隔离、loopback 鉴权、opaque 原生文件授权、凭据保密，以及“选择保存目标时取消则不创建任务”。
8. CLI 命令、HTTP 契约、生成类型、URL、导航顺序、语言策略指纹、提示词资源与干净安装行为不变。

## 6. 验证与首次检查结果

基线环境使用 `uv sync --locked --all-packages --group dev`。最初在工作区依赖未安装时无法导入 `wenyi_backend`；补齐后以下两组测试通过，合计 **47 + 41 = 88 项，无跳过**。

```bash
uv run --no-sync pytest -q \
  packages/core/tests/test_architecture_boundaries.py \
  packages/core/tests/test_orchestrator_contract.py \
  packages/backend/tests/test_backend_boundaries.py \
  packages/backend/tests/test_context_isolation.py \
  packages/backend/tests/test_repository_contracts.py

uv run --no-sync pytest -q \
  packages/core/tests/test_storage_injection.py \
  packages/core/tests/test_preparation.py \
  packages/core/tests/test_srt.py \
  packages/backend/tests/test_worker_tasks.py \
  packages/backend/tests/test_export_admission.py \
  apps/api/tests/test_desktop_route_isolation.py
```

这些结果只是首次审查时的有限基线，**不代表规划已经实现或全系统验证通过**。那次仅文档审查未执行前端 typecheck/build/E2E、完整 Python 测试、干净 wheel 安装、原生构建、PG/Redis 集成或模型质量评估；后续实现需另行验证。未使用私有书籍、项目状态或真实凭据。

后续实现遵循[仓库验证规则](../../../AGENTS.md#验证与完成)：Python 改动运行受影响测试、Ruff check/format 与 `git diff --check`；状态、锁、续跑及跨领域改动运行完整 Python 测试；共享 UI 改动验证两端构建、类型与受影响 Playwright。集成验证仅使用隔离的 `WENYI_TEST_DATABASE_URL`、`WENYI_TEST_REDIS_URL`，跳过必须记录为未验证。目录/包迁移还需干净安装、sdist/wheel 资源检查和非空架构扫描。只有另行批准的改动改变提示词或翻译语义时，才需要对应模型质量比较。
