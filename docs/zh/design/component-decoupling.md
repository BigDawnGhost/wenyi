# 功能组件解耦与目录规划

[English](../../design/component-decoupling.md) · [已实现的架构](../architecture.md)

## 状态与结论

**F1–F7 与 S0–S5 已全部实现。** 初次检查基线为 `80d448f`，初稿仅增加设计文档；本文现记录完整拆分结果。执行生命周期、后端显式依赖、输入准备与全书理解、用量所有权、UI 公共契约、共享验证及 CLI 文件迁移均已明确归属。临时 DAL、client 类型及宿主 fixture 转发已移除。见[当前模块职责](../architecture.md)、[CLI 流程](../cli.md#常用命令)及下方验证记录。

保留现有模块化单仓库与部署边界，先在**包内部**解耦职责，再整理目录。每一步迁移保留公开入口，只有调用方和测试全部迁移后才删除兼容转发。

| 方案 | 取舍 | 结论 |
|---|---|---|
| 只搬目录、拆小文件 | 文件看起来更短，但隐式依赖与重复规则仍在 | 不作为主要任务 |
| 按用例和消费者逐步抽取接口 | 改动易审查；兼容转发必须有明确删除条件 | **推荐** |
| 重建领域包或拆微服务 | 引入大量导入、打包与运维变化，当前没有对应部署需求 | 不纳入本次范围 |

本方案不要求迁移数据库或状态格式，不增加运行服务或 workspace 包，不修改提示词，也不重新设计界面。

## 1. 已经合理、应继续保留的边界

- Core 拥有翻译行为，CLI、共享 Backend 与平台适配依赖 Core，不反向依赖。
- [Orchestrator](../../../packages/core/wenyi_core/pipeline/orchestrator.py#L30) 已经将领域工作委派给服务。阶段路由和锁作用域是其合理职责，不应为了“更薄”继续拆散。
- [BackendContext](../../../packages/backend/wenyi_backend/context.py#L96) 按应用注入适配器；ContextVar 属于请求/任务作用域，**不是**进程级 Web/Desktop 开关。线程与回调中的上下文传播必须保留。
- Web 拥有 PostgreSQL 与 Redis/Arq；Desktop 拥有独立工作区、SQLite 目录库、凭据与本地运行器。PostgreSQL 适配器必须留在 `apps/api`，不能移入 Core。
- 共享 UI 已有 feature 目录与宿主能力注入。继续区分路由、外壳、基础组件与功能组件，不为更换父目录名称搬动所有页面。
- `ArtifactStorage`、Review 证据/模型、markup、DOCX 样式策略、语言策略及独立 SRT 执行已经形成有效边界，应复用而不是再造一套抽象。
- 已有 [Python 边界测试](../../../packages/backend/tests/test_backend_boundaries.py#L11)、[前端边界测试](../../../packages/ui/tests/platform-boundaries.test.mjs#L31) 和[干净安装隔离检查](../../../scripts/check_backend_packages.py#L19)。需要定向补强，而不是推倒重建。

## 2. 检查发现与建议拆分点

优先级表示实施顺序，不表示已确认的线上故障。文件长、存在装配对象或启动 singleton，本身都不构成缺陷。

### F1：任务入口与业务执行共用一个复杂生命周期

**原始发现：** `workers/tasks.py` 同时承担调度上下文、配置解析、持久化身份校验、取消、领域操作与遥测/存储清理。Web 与 Desktop 通过相同的 worker 形状入口调用。

**风险：** 修改调度调用或任务终态处理，会触碰业务执行链。迟到回调与重复投递的安全性依赖嵌在链路内的任务身份及锁检查。

**已落地的 P1 拆分：** [ExecutionRequest 与依赖](../../../packages/backend/wenyi_backend/execution/contracts.py#L20) 显式传入 [runner](../../../packages/backend/wenyi_backend/execution/runner.py#L64)，由后者统一拥有执行前身份复核、中断、清理和终态处理。[操作层](../../../packages/backend/wenyi_backend/execution/operations.py#L108) 负责预览、书籍和字幕用例；`workers/tasks.py` 保留原任务名称及签名，作为薄包装。Web Arq 与 Desktop 调度继续属于平台适配；CLI 仍直接调用 Core，不接入 HTTP 应用层。

runner 从持久任务身份复核、恢复直到领域执行结束持有书级长运行锁。成功目录库状态写入在实时统计清理与运行锁退出后进行，保留现有执行契约，而不是扩大到原方案设想的锁范围。资源取得后立即登记清理，包含启动失败路径；异步取消等待同步工作结束后才返回。读取路由也在成功与错误路径中关闭本次操作创建的存储。

同步调度器通过执行依赖注入：Web 保留默认 executor，Desktop 保留 `main` 新增的每工作流独立线程。集成该改动后，验证了上下文传播、并行关闭、暂停/失败隔离和续跑用量。

独立导出协调器在同一个 `export_lock` 内完成 claim、render、publish 与终态写入。书籍渲染使用 Core 一致快照和 assemble lock；SRT 保留独立的 state-lock 快照路径。渲染不更新目录库状态，发布不调用 Writer。复用现有导出计划、路径和响应辅助模块，不创建第二套导出策略。

### F2：作用域上下文正确，但动态转发掩盖服务的真实依赖

**原始发现：** 共享 DAL 通过当前 repository 动态转发任意属性，PostgreSQL repository 又转发到平台 DAL；聚合 repository 混合了不同消费者的能力。

**风险：** 服务能够在不修改输入契约的情况下获取更多持久化能力，动态转发也削弱了静态检查。

**已实现：** `ports/` 中的项目/任务协议显式传入执行、项目、准入与原文视图服务，只读投影使用只读契约。[执行装配](../../../packages/backend/wenyi_backend/execution/bindings.py#L63) 与 [HTTP 装配](../../../packages/backend/wenyi_backend/routers/dependencies.py#L19) 从 `BackendContext` 绑定依赖，业务服务不再通过隐式上下文寻找依赖。路由直接通过类型化 repository 执行目录库操作。共享动态 DAL 已删除，[PostgresRepository](../../../apps/api/wenyi_api/adapters.py#L28) 显式声明支持的方法，不再使用 `__getattr__`。

**不能**把逻辑上耦合的准入与补偿拆成无关仓储调用。现有 `PostgresRepository`、`LocalBackend` 实现多个小协议，SQL 保留在平台适配器。一般准入及补偿保留各平台原有边界，不新增共享跨存储事务。本次拆分另有两项独立回归测试保护的安全修复：暂停在目录库中原子复核当前运行身份/状态；原文替换先提交缓存失效，再发布原文引用。发布结果不确定时保留候选文件，不引入自动孤立文件清理。

### F3：输入准备同时承担解析、初始化和模型辅助的全书理解

**原始发现：** 准备流程与 Backend 预览重复维护解析参数、源文检查和解析文档缓存规则，准备流程还同时协调全书理解的并发。

**风险：** 解析参数、缓存身份和源文变更处理有多个需要同步维护的调用方；生成预览不应依赖一个已经装配模型的 runtime。

**已实现：** 共享输入准备 API 集中管理解析参数、源文身份检查与已验证缓存文档，支持缓存时通过注入的产物接口访问。Backend 保留 HTTP 预览数据投影；`PreparationService` 保留初始化、阶段协调和 manifest 提交职责。调用方迁移后，旧解析转发方法已移除。

保留各调用方原有缓存策略，包括 PDF 转换前已知状态目录并写初始化标记、其他格式按书名定位的区别。[BookUnderstandingService](../../../packages/core/wenyi_core/pipeline/book_understanding.py#L34) 独立拥有 digest/synopsis 协调、稳定顺序并发和可续跑检查点，只接收配置、Agent 与用量协调器，不接收整个 Runtime；源文 hash、缓存版本和语言策略指纹不变。

### F4：运行时装配同时拥有可恢复的用量发布

**原始发现：** Runtime 在构造 client 和 Agent 的同时，还更新 client 检查点并发布书籍与 Review 账本；把它传给拆出的服务，会暴露超出实际记账需求的依赖。

**风险：** 看似简单的账本代码搬移可能改变检查点时机、恢复或只计费一次语义。如果拆出的服务继续接收整个 runtime，耦合并没有消失。

**已实现：** [UsageCoordinator](../../../packages/core/wenyi_core/pipeline/runtime_usage.py#L28) 拥有唯一 client 检查点，显式依赖用量源、书籍账本与 Review 账本协议，所有书籍工作流消费者共享同一所有者。先恢复再读取累计值，先持久化绝对累计量日志再推进内存检查点，重试不会重复应用增量。原有计算、空 Review 账本行为、事件载荷和存储格式保持不变。

不预建没有消费者的端口。语言绑定和计时仍是 Runtime 的合理职责；`RunStore` 与聚合 `Storage` 契约保留。独立 SRT 记账不变。

### F5：UI 契约暴露实现路径，并存在仅类型层面的反向依赖

**原始发现：** 平台契约从 API 实现导入类型，通配包导出与宿主别名暴露共享 UI 内部路径。这属于类型反向依赖及公共 API 设计问题，不表示已确认运行时循环故障。

**风险：** 移动私有 UI 模块会连带修改宿主，基础契约又依赖其服务的 client 实现。这是公共 API 边界问题，不是“宿主依赖共享 UI”这个方向有错。

**已落地的 P1 拆分：** [API 类型](../../../packages/ui/src/api/types.ts#L1) 仅依赖生成的 schema，由 client 与[平台契约](../../../packages/ui/src/platform/contracts.ts#L1) 共同消费。传输、进度、偏好、活动和可选能力组合成较小接口，唯一 client、React 挂载前配置和 `platform()` 门面的行为保持不变。

[显式包导出](../../../packages/ui/package.json#L6) 覆盖 app、platform、API/类型、HTTP、i18n、文档语言、样式、允许复用的基础组件、设置布局和格式化。TypeScript 与 Vite 均遵守这些导出，宿主 `@/` 仅指向自身 `src`，共享 UI 使用相对导入。[导入解析检查](../../../packages/ui/tests/public-surface.test.mjs#L14) 拒绝私有子路径、相对路径越界及别名绕过，并包含故意构造的越界用例。

唯一 client 已迁入 `api/client.ts`，消费者从 `api/types.ts` 导入类型；旧 `lib/api.ts` 实现/类型转发入口已删除，公共 `@wenyi/ui/api` 导出不变。

查询和 mutation 仍由对应 feature 拥有。按真实消费者逐步拆分 endpoint 模块，不新增第二个状态缓存、通用 service 框架或独立 API-client 包。

### F6：部分共享验证仍归属于某个宿主，或重复维护契约

**原始发现：** Desktop 导入 Web 的 fixture 与外壳测试，产物检查重复维护路由 chunk 名称，schema 生成依赖运行中的 API 且缺少 CI 一致性检查。

**风险：** 共享测试改动意外地由 Web 持有；新增路由或修改 schema 时，重复的测试/类型输入可能未同步。这些是护栏缺口，不表示当前类型或路由已经出错。

**已落地的护栏：** [离线 schema 导出](../../../scripts/export_openapi.py#L21) 使用共享 app factory 与不可调用的运行端口，不启动生产 lifespan。`pnpm check:schema` 生成临时类型并与已提交文件比较，CI 在前端检查前执行。[路由 loader 检查](../../../packages/ui/tests/source-contracts.mjs#L43) 从 manifest 推导 chunk，同时保留独立的必要 URL/重定向断言及既有 audience/外壳 E2E 覆盖。

**已完成的测试归属迁移：** 全部 20 个跨宿主套件、API fixture、select 辅助函数与测试 harness 均归 UI 测试支持目录，各宿主用自身 bootstrap 和能力选项显式注册，不再导入另一宿主的测试。宿主 fixture 转发已删除。边界检查要求两端注册每个共享套件，并禁止共享测试支持包含原生 bootstrap。原生保存/拖入/凭据/bootstrap 与浏览器行为继续分开验证，测试支持不进入运行时导出。

### F7：CLI 迁移命令直接调用文件存储私有方法

**原始发现：** CLI 迁移命令了解账本路径、自行创建备份，并调用 `RunStore._write_json`。

**风险：** 存储内部重构可能破坏用户可见的迁移命令。

**已实现：** [migrate_usage_ledgers](../../../packages/core/wenyi_core/storage/file_migrations.py#L13) 拥有枚举、校验、原始字节备份、运行锁，以及通过公开文件存储接口执行的原子写入。复用纯账本转换函数，先校验全部候选，再修改账本。重试跳过已转换项；发布中断保留原文件及其备份。CLI 只负责参数校验和展示，不发现或迁移 Web/Desktop 状态。

无关 CLI 命令重组不属于本方案范围。

## 3. 目标目录

下图是**已实现的选定模块结构**，不是完整目录树。未列出的模块不动，每个抽取模块都有实际消费者。

```text
packages/
  core/wenyi_core/
    pipeline/
      orchestrator.py                  # unchanged facade
      preparation.py                  # initialization and stage coordination
      input_preparation.py            # extracted parsing/cache rules shared with preview
      book_understanding.py           # digest/synopsis concurrency and checkpoints
      runtime.py                      # composition, language binding and timing
      runtime_usage.py                # run-owned usage checkpoint/publication
      runstore.py                     # unchanged file-state compatibility layer
    storage/
      protocol.py                     # retained; consumer-sized protocols as needed
      file.py / sqlite.py             # retained adapters
      file_migrations.py              # public CLI file migration operation
  backend/wenyi_backend/
    application.py / context.py       # retained HTTP and adapter composition
    ports/
      projects.py / jobs.py           # implemented consumer-sized persistence contracts
    execution/
      contracts.py                    # implemented typed run request and dependencies
      runner.py                       # implemented lifecycle and resource ownership
      operations.py                   # implemented preview/book/SRT dispatch into Core
      exporting.py                    # implemented rendering/publication separation
      bindings.py                     # implemented context-to-port composition
    workers/tasks.py                  # implemented stable task entry-point wrappers
    routers/                          # retained HTTP translation layer
      dependencies.py                 # explicit HTTP-to-service dependency binding
    export_plan.py / export_paths.py  # retained, not duplicated
  ui/
    src/
      App.tsx / routes/              # retained shell and routing
      platform.ts                    # implemented stable host entry point
      platform/contracts.ts          # implemented composable host capability contracts
      platform/runtime.ts            # implemented pre-mount configuration and access
      api/types.ts                   # implemented aliases derived from generated schema
      api/client.ts                  # the single client, with no type forwarding
      components/ui/index.ts         # implemented approved host-facing primitives
      features/                      # retained; feature-local queries/components
      i18n/                          # retained
    tests/e2e/
      fixtures.ts                    # shared API fixtures
      shell.ts / ...                 # 20 explicitly registered cross-host suites
      select-helper.ts / select-harness.tsx
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
| S0：护栏（完成） | schema 一致性、导入解析/路由检查、后端服务边界和包内容检查 | 扫描非空；人为构造的越界导入能被拦截；fake-port schema 导出不接触外部服务；两端构建先于 bundle 检查 |
| S1：UI 公共边界（完成） | F5/F6：公共类型/exports、全部共享 fixture/套件/辅助函数，宿主拥有 bootstrap | 两端 typecheck/build、边界测试和 E2E 通过；URL、query key、DOM/外壳、原生能力与凭据行为不变 |
| S2：后端执行（完成） | F1/F2：生命周期 runner、消费者端口、显式应用服务与平台适配；稳定 worker 包装 | 重复/过期任务、暂停取消、清理补偿、锁竞争、重复导出及发布/终态失败；并发 app/worker 隔离和线程/回调传播；隔离 PG/Redis 与 Desktop runner 契约 |
| S3：输入准备与全书理解（完成） | F3：无模型 parser/cache API 和独立全书理解服务 | 缓存命中/失效、配置/源文不匹配、PDF 失败重试、manifest 最后提交、无模型预览、稳定顺序及预扫中断续跑 |
| S4：账本与文件迁移（完成） | F4/F7：唯一用量检查点所有者和公开本地文件迁移操作 | 崩溃/重试证明书籍与 Review 用量只合并一次；日志/事件失败、备份和原子发布重试；完整 Python 与存储适配验证 |
| S5：移除兼容转发（完成） | 删除共享 DAL、旧解析辅助入口、client 类型转发和宿主 fixture 包装 | 废弃入口无剩余调用；文档、测试和扫描器一致；干净安装检查确实发现实际模块和资源 |

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

## 6. 验证记录

### 首次仅文档审查

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

### 完整实现验收

集成 `main` 的 `d29acc7` 后，本地验证使用 Python 3.12、Node 22/pnpm 9、临时 fixture 状态与一次性 PostgreSQL/Redis 服务。集成保留上游 Desktop 独立工作流调度器、简化的桌面工作流界面与项目报告退役行为；两个新增跨宿主套件使用共享注册边界，已退役报告请求返回 404 且不打开存储。

| 检查 | 结果 |
|---|---|
| 配置隔离测试数据库/Redis URL 后运行 `uv run --no-sync pytest -q` | 2757 项通过、72 个 subtest 通过、5 项跳过；包含架构、存储、恢复、API 集成及 Desktop 生命周期覆盖 |
| 两端 `typecheck`、`typecheck:tests`、`build` | 全部通过 |
| 两端完整 `test:e2e --workers=2 --max-failures=1`，通过临时 `--config` 隔离端口与结果目录 | Web 312 项通过、Desktop 272 项通过；使用 bundled Chromium 153.0.8010.12 |
| 两端构建后运行 `pnpm check:schema`、`pnpm test:frontend-boundaries` | schema 未漂移，13 项边界测试通过 |
| 全范围 Ruff check/format、`pyright --project pyrightconfig.json --warnings`、`git diff --check` | 通过；Pyright 1.1.412 在配置的源码/测试范围内零错误、零警告 |
| 五个 Python 包 sdist/wheel 构建，资源/版本与干净安装检查 | 通过；验证了 66 个语言资源及 CLI/shared/Web/Desktop 依赖隔离 |
| 新环境默认 `uv sync --locked` | 仅安装 CLI/Core 产品包；刷新 editable 包元数据后，与构建产物的 Git 派生版本一致 |

共享 API mock 已从匹配任意层级的 `api` 目录改为匹配根 `/api/` 路径。回归测试在修复前复现前端源码模块被拦截，修复后通过。Desktop 首轮有一次原文视图等待超时，单独复跑和随后带 trace 的全量复跑均通过，未放宽断言；此处记录为偶发测试现象，不据此声称修复了另一个产品缺陷。

五项 Python 跳过分别为三项 Windows 专属用例、需显式启用的 Nginx 代理集成、缺少可选 Weaver PDF 样本。本地未执行原生 Tauri 打包及其他操作系统/Python 矩阵，由 CI 单独验证；未执行真实模型/解析服务质量对照。本次抽取不改变提示词或翻译语义；未使用私有书籍、既有项目状态或真实凭据。

后续实现遵循[仓库验证规则](../../../AGENTS.md#验证与完成)：Python 改动运行受影响测试、Ruff check/format 与 `git diff --check`；状态、锁、续跑及跨领域改动运行完整 Python 测试；共享 UI 改动验证两端构建、类型与受影响 Playwright。集成验证仅使用隔离的 `WENYI_TEST_DATABASE_URL`、`WENYI_TEST_REDIS_URL`，跳过必须记录为未验证。目录/包迁移还需干净安装、sdist/wheel 资源检查和非空架构扫描。只有另行批准的改动改变提示词或翻译语义时，才需要对应模型质量比较。
