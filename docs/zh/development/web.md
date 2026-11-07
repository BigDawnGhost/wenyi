# Web 开发

本文说明 Web 的开发环境和后端契约。共享浏览器 UI 规范见[前端开发](frontend.md)；领域检查点、Review 语义和用量记账见[流水线开发](pipeline.md)。用户操作统一写在[工作台指南](../guide/workspace.md)。

<a id="local-development"></a>

## 本地开发环境

使用 Python 3.10+、`uv`、Node 22、pnpm 9、PostgreSQL 16 和 Redis 7。命令从仓库根目录执行。默认 Python 工作区安装仅选择 CLI/Core，开发 Web 需安装完整 workspace：

```bash
uv sync --locked --all-packages --group dev
pnpm install --frozen-lockfile
```

后续使用 `uv run --no-sync`，避免命令重新同步默认包集合并移除 API 依赖。

启动专用开发服务，不使用生产数据库或 Redis。开发覆盖文件仅发布回环端口：

```bash
test -f deploy/.env || cp .env.example deploy/.env
docker compose -p wenyi-dev \
  -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml \
  up -d postgres redis
```

`-p wenyi-dev` 为这套服务分配独立命名卷。确保 5432 和 6379 端口空闲，否则调整本地绑定及连接 URL。后续服务命令继续使用同一 project 名称和两个 Compose 文件，停止服务时不要删除数据卷。

在每个后端终端中设置应用环境：

```bash
export DATABASE_URL=postgresql://wenyi:wenyi@localhost:5432/wenyi
export REDIS_URL=redis://localhost:6379/0
export DATA_DIR=./data/web-dev
export WENYI_CONFIG=config.yaml
```

API 的模型测试与流程 Worker 的模型任务需要对应模型密钥；执行 MinerU 解析的进程需要
`MINERU_API_KEY`。导出 Worker 需要共享服务和数据，导出 PDF 时还需访问 BabelDOC，
但不需要模型或 MinerU 密钥。当前 Compose 在容器间共用 `env_file`，不会将其自动导出到
宿主 shell。测试鉴权访问时，使 `WENYI_API_TOKEN`、`WENYI_CORS_ORIGINS` 与浏览器配置匹配。

在四个独立终端中启动：

```bash
# API
uv run --no-sync uvicorn wenyi_api.main:app --reload --port 8000

# Workflow queue
uv run --no-sync arq wenyi_api.workers.WorkerSettings

# Export queue
uv run --no-sync arq wenyi_api.workers.ExportWorkerSettings

# Browser frontend
pnpm -C apps/web dev
```

打开 <http://localhost:5173>。Vite 将 `/api` 去掉前缀后转发到 API，`/ws` 作为 WebSocket 转发。API 会初始化当前数据库 schema，不迁移旧版 Web 状态。流程与导出使用独立队列，只启动普通 Worker 会让导出一直排队。

<a id="mock-api"></a>

## Mock API 开发

不启动 API、数据库、Worker 或模型服务也可查看浏览器界面：

```bash
MOCK_API=1 pnpm -C apps/web dev
```

Vite 通过 `packages/ui/sandbox/fixtures.ts` 中与 Playwright 共用的 fixture 应答 `/api`。操作作用于 mock 状态，不接触真实项目。Mock 不提供进度 WebSocket，界面使用 HTTP 后备读取。不设置 `MOCK_API` 时，恢复正常代理开发。

Mock 成功只能证明浏览器行为，不能证明鉴权、存储事务、队列恢复、解析服务可用性或翻译质量。这些保证需对应后端／集成测试验证。界面布局、国际化、共享 fixture、平台隔离和分包体积规范见[前端开发](frontend.md)。

<a id="http-contract"></a>

## HTTP 契约与 schema 生成

`packages/backend/wenyi_backend` 的共享 HTTP app 和 schema 定义契约，`apps/api/wenyi_api` 注入 PostgreSQL、Redis 和 Arq 适配器。Core 不依赖 HTTP 或这些 Web 适配器，共享 backend 不导入 Web／Desktop 宿主。

直连 API 的路由不带 `/api` 前缀；Web 代理向浏览器提供该前缀。WebSocket 路由仍为 `/ws/projects/{pid}/progress`。运行时可通过 `/docs`、`/openapi.json`、`/capabilities` 检查接口，并遵循部署鉴权。

修改路由或 schema 后，离线重新生成契约：

```bash
pnpm gen:schema
pnpm check:schema
```

生成需要已安装的 Python／前端依赖，不需要运行中的 API、PostgreSQL、Redis 或凭据。仅用于 schema 的 app 装配不进入生产 lifespan，也不调用运行端口；比较时只标准化依赖安装版本的版本字段。

- `packages/shared-schema/src/api.d.ts` 来自 `wenyi_backend/schema_export.py`，包含共享路由。
- `packages/shared-schema/src/desktop.d.ts` 来自 `wenyi_desktop/schema_export.py`，包含 Desktop 追加路由。

不要手改生成类型，也不要把 Desktop 凭据／IPC 路由放入共享契约。`check:schema` 比较临时生成物，不改写受版本控制的文件。

### 鉴权与错误

`WENYI_API_TOKEN` 启用共享 token 鉴权，不提供独立用户授权。HTTP 客户端使用 Bearer 鉴权；健康检查路由仍可无 token 访问。CORS 配置约束 HTTP 浏览器访问，不是 WebSocket origin 授权机制。公开部署还需落实[部署指南中的网络控制](../guide/web.md#authentication-and-network)。

订阅进度时：

1. 连接 `/ws/projects/{pid}/progress`。
2. 在十秒内发送 JSON 首包 `{"token":"…"}`。服务端未配置 token 时也发送，此时值可为空。
3. 服务端先检查配置的 token 与项目存在性，再发送快照和事件。鉴权超时、token 不匹配及项目不存在的处理分支使用 `1008` 关闭连接。

带错误码的 HTTP 失败响应包含 `code`、`message` 和可选的 `request_id`，不通过 `detail` 重复文案。客户端按 `code` 分支，不匹配英文句子。未编码的 HTTP 异常保留 FastAPI 的 `{"detail": ...}` 形态，客户端也须兼容。

HTTP 响应包含 `X-Request-ID`，服务端失败还包含 `X-Error-ID`，可据此关联服务端日志。Docker 日志配置输出 JSON，其他入口可用 `WENYI_LOG_FORMAT=json` 选择该格式。内部 traceback 留在运维日志中，不通过项目事件返回；读取历史事件时也会过滤。

<a id="progress-contract"></a>

## 进度身份与实时统计

把项目身份和运行身份视为事件契约的一部分：

- 每个订阅属于一个项目。运行级进度、统计、状态和错误事件携带 `project_id`、`run_id`；客户端不得把其他项目的事件或旧运行输出套用到当前流程。
- 首个 `snapshot` 携带项目／章节状态，`heartbeat` 只是传输心跳而非任务更新。这两种消息不应被强制要求与运行级事件具有相同结构。
- 最近一次进度回调在 Redis 中缓存七天，并带运行身份。须与最新的非导出流程核对，不能仅匹配项目 ID。阶段和计数来自真实回调，不根据启用的配置推测完成情况。
- 三十秒没有其他帧时，服务端发送心跳。共享客户端把六十秒静默视为断线，重连后刷新已保存状态。HTTP 读取负责后备与核对；WebSocket 不是持久任务账本。
- 实时统计是绑定运行的临时 Redis 快照。过期统计不能继续推动显示时钟。最终累计用量和计时来自持久状态；传输快照和缓存计数不能直接确定费用。

构造测试事件时须使用界面显示项目的真实当前运行身份。修改进度行为应覆盖重连、旧事件和终态核对，不能只断言某个标签发生变化。

<a id="execution-and-recovery"></a>

## 执行、锁与恢复

### 状态与执行归属

PostgreSQL 是 Web 可变项目状态的唯一存储。`DATA_DIR` 保存上传原文、解析资源／缓存和导出产物，不另存章节、术语、Review 检查点或用量的 JSON／SQLite 副本。存储适配器需保持共享[流水线不变量](pipeline.md)。

API 在入队前接纳并持久化任务。流程接纳时捕获该次运行的有效配置，Worker 执行已保存快照。项目／运行身份须贯穿接纳、执行、回调、暂停／恢复和终态；迟到完成或重复投递不能覆盖较新的流程。

`BackendContext` 注入应用拥有的仓库、存储、遥测、模型构造和队列端口。ASGI 请求、WebSocket 处理器、Worker 入口和恢复循环均绑定自己的上下文。`asyncio.to_thread` 与 AnyIO 会传播上下文，原生线程需显式复制。不要新增进程全局的「当前平台」开关，也不要让回调误用其他应用的适配器。

长流程使用 PostgreSQL **会话 advisory lock** 保持项目执行归属。状态修改使用短事务与事务级 advisory lock。模型请求期间可持续持有会话锁，但不需保持数据库写事务。模型请求、解析及渲染不得塞进短状态事务。

导出有持久任务／导出身份和独立执行锁。保存的一致快照允许渲染与翻译并行，不必持有项目长时间写锁。重复投递不得再次生成已完成导出。

取消时须等待同步执行及检查点写入收尾，之后才能释放执行归属。流程终态须在退出运行锁后发布；成功完成还要等完成事件持久化和实时统计收尾。观察到流程终态时，不应仍有旧 Worker 占用保护编辑的运行锁。

### 恢复不是总运行时长超时

每个 Worker 启动独立异步恢复循环，即使全部流程槽位被占用，维护仍可运行。每三十秒检查超过两分钟未更新的排队／运行任务：

1. 非阻塞尝试获取对应的项目或导出会话锁；其他 Worker 仍持锁时不处理该任务。
2. 持锁重新读取任务。排队任务在判定投递丢失前，还须核对对应 Redis 队列。
3. 确认失联的流程记为 `interrupted`，仅当它仍是该项目最新流程时暂停项目。恢复继续原任务类型和已保存进度。
4. 失联导出的任务与导出记录记为 `error`，由用户重新创建导出。

两分钟只是恢复候选记录的年龄阈值，不是失败证据，也不是合法任务的最大运行时长。Arq 执行上限与此恢复规则分开。修改调度或恢复时必须保留活跃锁检查；一次长模型调用可能不更新任务行，但仍然健康。

<a id="metrics"></a>

## 运维指标

`GET /metrics` 输出 Prometheus 文本，使用与其他受保护 HTTP 路由相同的 `WENYI_API_TOKEN` 鉴权。开启鉴权时为采集器配置 bearer token；Wenyi 不自动安装采集器。

| 指标 | 含义 |
|---|---|
| `wenyi_tasks_total{status,code}` | 结束的流程任务，结果为 `done`、`paused` 或 `error`；不包含导出 |
| `wenyi_llm_retries_total{provider,reason}` | 实际重试等待，按 call ID 与 attempt 去重；耗尽通知不另计一次重试 |
| `wenyi_queue_depth` | PostgreSQL 任务目录中已接纳、仍排队的流程和导出任务，不含运行中任务 |

API 与 Worker 必须共享 Redis 数据库才能看到共同计数。计数保留时间取决于 Redis key；最近任务／重试身份使用 20,000 条有界去重窗口，不是无限期的 exactly-once 历史。计数写入尽力完成，不能令流程失败。计数存储或任务目录不可用时，采集请求返回 HTTP 503，而非伪造的零值快照。

**这些指标用于运维观察，不是持久 token 用量或费用账本。** 样本缺失、Redis 重置及有界去重使其不能用于重建收费。持久记账见[流水线开发](pipeline.md)。

<a id="validation"></a>

## 验证要求

使用临时项目与假的模型／解析服务，普通回归 fixture 不使用私有书籍或真实付费模型请求。后端修改先运行受影响测试与风格检查：

```bash
uv run --no-sync ruff check packages/backend apps/api
uv run --no-sync ruff format --check packages/backend apps/api
uv run --no-sync pytest -q -n auto -m "not integration" packages/backend/tests apps/api/tests
git diff --check
```

状态、锁、续跑或跨领域修改应扩大到完整 Python 测试集，离线测试保留 `-n auto`。模块边界修改需运行 `packages/core/tests/test_architecture_boundaries.py`；pipeline 装配／依赖修改还需 `test_orchestrator_contract.py`；存储适配修改需 `test_storage_injection.py` 及对应真实后端测试。

### PostgreSQL 与 Redis 集成测试

准备隔离测试服务，同时设置 **`WENYI_TEST_DATABASE_URL` 和 `WENYI_TEST_REDIS_URL`**。PostgreSQL fixture 会创建并在结束时删除独立 schema，还可能创建 `pg_trgm` 扩展。Redis 测试会写入和清理测试 key。这些保护措施不意味着可以对生产服务运行测试。

例如，先在独立本地端口准备可丢弃的数据库和 Redis，再执行：

```bash
WENYI_TEST_DATABASE_URL=postgresql://wenyi:wenyi@127.0.0.1:55432/wenyi_test \
WENYI_TEST_REDIS_URL=redis://127.0.0.1:56379/1 \
uv run --no-sync pytest -q -n 2 -m integration apps/api/tests
```

与 CI 一样将集成测试限制为两个 Worker，控制数据库连接数。缺少服务变量时相关用例会跳过，交付时须明确报告，不能称为存储／队列验证通过。

### Schema 与浏览器检查

接口契约、鉴权或进度变更需同时检查后端和浏览器行为：

```bash
pnpm check:schema
pnpm -C apps/web typecheck
pnpm -C apps/web typecheck:tests
pnpm -C apps/web build
pnpm -C apps/web exec playwright install chromium
pnpm -C apps/web test:e2e
```

Playwright 在回环地址 4173 端口启动 Vite，用 mock HTTP／WebSocket 响应验证浏览器契约，不能代替服务集成测试。共享 UI 变更还需 Desktop typecheck／build／E2E，并在两端构建后运行 `pnpm test:frontend-boundaries`，见[前端开发](frontend.md)。真实模型质量评估与离线／浏览器检查分开报告。

<a id="docker-builds"></a>

## Docker 开发构建

普通部署 Dockerfile 不要求 Buildx 缓存挂载。后端镜像仅安装 Web 依赖闭包，不包含 CLI／Desktop 运行环境；修改打包时保留该平台边界。

需要可选的带缓存构建时，安装 Buildx 插件，然后在 `deploy/` 执行：

```bash
DOCKER_BUILDKIT=1 COMPOSE_BAKE=true docker compose \
  -f docker-compose.yml -f docker-compose.buildx.yml build &&
  docker compose -f docker-compose.yml -f docker-compose.buildx.yml up -d --no-build
```

覆盖文件选择带缓存的 Dockerfile，保留服务配置及卷。修改安装步骤时保持普通版与缓存版一致。用 `&&` 连接构建和启动，防止构建失败后无意启动旧镜像。

Web 代理通过 Docker DNS 重新解析 API 上游，HTTP 与 WebSocket 共用，DNS 缓存五秒。运行环境需 Nginx 1.27.3 或更新版本以支持动态上游解析。修改代理或镜像配置后，应验证 API 容器重建无需重启 Web。
