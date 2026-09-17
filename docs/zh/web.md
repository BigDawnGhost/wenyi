# Web 部署与开发

[English](../web.md) · [CLI 使用](usage.md) · [配置](configuration.md)

Web 使用 React/Vite、FastAPI、Arq、PostgreSQL 和 Redis。普通任务与导出由两个独立 Worker 消费；领域逻辑来自 `wenyi_core`，不把 CLI 文件状态布局复制进数据库。

## 全新 Docker 部署

需要 Docker 与 Compose v2。在仓库根目录执行：

```bash
cp .env.example deploy/.env
# 编辑 deploy/.env，为 config.yaml 所选提供商填入密钥。
docker compose -f deploy/docker-compose.yml --profile full up --build
```

| 服务 | 地址 / 用途 |
|---|---|
| Web | http://localhost:8080 |
| API / OpenAPI | http://localhost:8000 / http://localhost:8000/docs |
| `worker` | `wenyi:workflows`：解析、准备、翻译、Review、SRT |
| `export-worker` | `wenyi:exports`：快照导出 |
| PostgreSQL / Redis | 默认仅容器内网 |

仅后端使用 `--profile server`。该模式用于自建前端或 API 客户端；本地 CLI 自己运行核心，不调用这个服务器。

```bash
docker compose -f deploy/docker-compose.yml --profile server up --build
# 后台运行可加 -d；停止保留数据：
docker compose -f deploy/docker-compose.yml --profile full down
```

本次数据库结构面向全新部署，不执行旧项目/策略/数据库迁移。保留旧部署时，新版使用独立 Compose project、数据库和卷，例如启动时加 `-p wenyi-new`。不要对需要保留的数据卷执行删除操作。

## 共享配置与凭证

API、普通 Worker、导出 Worker 都加载 `deploy/.env`，只读挂载同一 `config.yaml`，并共享 `/data`。更改提供商环境变量后重建相关容器；修改配置后新任务读取新默认值，项目显式设置仍优先。

| 环境变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` 等 | 只需设置实际路由涉及的提供商凭证；自定义提供商在 `.env` 增加 `api_key_env` 指定的名称。 |
| `MINERU_API_KEY` | 仅使用 MinerU PDF 解析时需要。 |
| `WENYI_CONFIG` | 应用读取的核心配置路径；容器固定 `/app/config.yaml`，本地默认 `config.yaml`。 |
| `WENYI_CONFIG_FILE` | Compose 宿主配置文件，默认 `../config.yaml`，相对 `deploy/`。 |
| `DATA_DIR` | 上传原件、解析资源与导出成品目录；容器固定 `/data`。 |
| `DATABASE_URL` / `REDIS_URL` | 本地开发连接地址；Compose 覆盖为内部服务地址。 |
| `INSTALL_PDF_OUTPUT` | Docker 构建参数，默认 `true`，安装 WeasyPrint 和 fpdf2；`false` 省略 Python PDF 输出依赖。 |
| `WENYI_API_TOKEN` | 可选静态 token；HTTP 使用 Bearer 鉴权，WebSocket 使用连接后的首个 `{"token":"…"}` 消息鉴权，前端自动发送。 |
| `WENYI_CORS_ORIGINS` | 允许的源，逗号分隔；默认 `*`。 |

自建 WebSocket 客户端须在连接后 10 秒内发送 JSON 首包 `{"token":"…"}`；未配置服务端 token 时也发送首包（token 可为空）。通过鉴权后才接收项目快照和实时事件。

数据库存储项目、章节、段落、术语、Review 证据/检查点、Autofix 发布索引、字幕缓存、用量和计时。`DATA_DIR` 不存这些可变状态的 JSON/SQLite 副本。

### PDF

后端镜像默认包含 WeasyPrint/fpdf2、所需 Pango 库、Noto 中日韩/通用字体和文泉驿正黑。fpdf2 仅使用带 TrueType 轮廓的字体，自动跳过 Noto CJK 等 OpenType/CFF 字体；也可通过 `TRANS_NOVEL_PDF_FONT` 指定兼容字体。显式选择不兼容字体会返回错误，避免生成显示损坏的文件。WeasyPrint 可使用 Noto CJK。MinerU 为默认解析后端，解析缓存与上传预览共用。

BabelDOC 作为独立 HTTP bridge 部署。将 `pipeline.pdf_backend` 设为 `babeldoc`，把 `pipeline.babeldoc_bridge_url` 设为 API 与两个 Worker 都能访问的地址。bridge 的 `127.0.0.1` 指当前容器，不是宿主机。此 Compose 不自动下载或启动 BabelDOC 服务。页面根据已安装依赖/配置呈现可用 PDF 功能。

## 本地开发

需要 Python 3.10+、`uv`、Node 22、pnpm 9，以及 PostgreSQL 16 / Redis 7。先启动数据库，开发覆盖文件显式绑定宿主机回环端口：

```bash
cp .env.example deploy/.env
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml \
  --profile server up -d postgres redis
uv sync --all-packages --group dev
pnpm install --frozen-lockfile
export DATABASE_URL=postgresql://wenyi:wenyi@localhost:5432/wenyi
export REDIS_URL=redis://localhost:6379/0
export DATA_DIR=./data
export WENYI_CONFIG=config.yaml
export DEEPSEEK_API_KEY=your-key
```

在四个终端中使用相同应用环境变量：

```bash
uv run uvicorn wenyi_api.main:app --reload --port 8000
uv run arq wenyi_api.workers.WorkerSettings
uv run arq wenyi_api.workers.ExportWorkerSettings
pnpm -C apps/web dev
```

Web 开发地址为 http://localhost:5173，Vite 代理 `/api` 与 `/ws` 至 API。API 初始化全新数据库结构。仅起普通 Worker 不会消费导出队列。

## 使用流程

1. 先选择源／目标语言及非空 EPUB、DOCX、FB2、TXT、Markdown、HTML、PDF 或 SRT 文件，再创建项目。书籍可选勾选「译前准备」；PDF 解析器在上传前选择。
2. 上传后解析作为后台任务运行，完成后显示预览；匹配的解析结果在准备时复用。
3. 项目继承全局「设置」的默认流程，创建页不提供流程选择器。需要微调时进入「项目配置」调整步骤并选用已注册模型，启动翻译前可校验实际路由。
4. 开始执行，在进度页查看状态；安全边界暂停后用恢复继续实际任务类型。
5. 书籍可编辑术语、风格、段落并查看全书审校历史、建议和实际修复记录；SRT 显示字幕条目及时间戳编辑入口。
6. 导出选择格式和单语/双语，独立 Worker 读取已保存快照。每次导出有独立文件位置，完成后下载。

**人工校阅**有独立导航入口，与全书审校分开。章节列表包含未完成章节；详情页每 3 秒读取已落盘的段落，每批译文保存后即可查看，无需等整章完成。待翻译段落显示“等待译文落盘”，有意保存的空译文仍计为完成。任务运行时可查看，暂停后可编辑已保存段落。自动刷新保留正在编辑的草稿。

段落右键菜单、改动记录含义、审校进度与证据，以及导出保留规则，详见[界面操作指南](web-interface.md)。审校页区分建议与实际写回，历史运行不会混入当前任务进度。服务端每个项目只保留最近五份成功导出的文件。

事件日志按最新在上的顺序展示，每 5 秒自动刷新。

标准默认开启预理解、润色、审校和自动修复。快速出稿关闭这四项。Review 指纹一致时复用已完成结果或恢复中断运行；关闭 Autofix 可保留建议而不正式发布。

同一项目的写入操作互斥：重复启动、执行中改配置/正文等冲突会返回明确错误。导出使用短时一致快照，可与翻译并行。项目初始化后要换目标语言或源内容应新建项目。

### Worker 异常退出后的恢复

Worker 启动独立的异步恢复循环，每 30 秒检查一次数据库中超过 2 分钟未更新的排队/运行任务。检查先尝试获取对应会话锁，确认没有仍在执行的 Worker；正常持锁的长任务不会仅因运行时间长而被判为失联。排队任务还会核对 Redis 队列状态。

确认失联后，普通工作流记录为中断，所属最新工作流项目置为 `paused`，可通过“恢复”继续原任务类型和保存进度。导出任务和导出记录置为 `error`，重新创建导出即可重试。导出拥有持久化任务身份及独立执行锁，防止重复投递重复生成文件，并支持异常退出后的状态恢复。

## 接口、检查与排错

运行中的 `/openapi.json` 是接口类型源，`GET /capabilities` 提供语言、格式、提供商和操作注册信息。生成前端类型：

```bash
pnpm gen:schema  # API 已在 localhost:8000 运行
uv run --no-sync ruff check packages/core packages/cli apps/api
uv run --no-sync pytest -q
pnpm -C apps/web typecheck
pnpm -C apps/web build
pnpm -C apps/web exec playwright install chromium
pnpm -C apps/web test:e2e
```

设置 `WENYI_TEST_DATABASE_URL` 运行真实 PostgreSQL 集成测试，测试会创建独立 schema 并在结束时删除它；使用测试数据库。CI 同时提供 Redis 服务。

- 任务一直排队：确认普通/导出 Worker 与 API 使用同一 Redis，并监听对应队列。
- PDF 解析失败：检查所选解析后端的凭证、bridge 地址及服务可用性。
- 缺少模型凭证：校验实际操作路由涉及的提供商环境变量，不必填写未使用提供商。
- 本地连不上数据库：使用 `docker-compose.dev.yml` 开放回环端口，或连接独立安装的数据库。
- 旧配置字段错误：按配置页校验结果移除旧 QA、回译、字符预算等字段；当前 Web 不提供旧项目自动迁移。

可用 `docker compose -f deploy/docker-compose.yml logs -f api worker export-worker` 查看任务失败原因。离线/浏览器验证结果与真实模型的翻译质量评估应分别记录。

### 提供商设置与流程视图

全局 **设置** 统一管理提供商连接、模型注册、默认档位与步骤路由，以及默认流程模板。连接和模型 ID 可以重命名，存在引用的条目不能删除；恢复默认配置先载入草稿，保存后生效。**项目配置** 只选用已注册模型并调整本项目流程，不注册提供商或模型。高级 YAML 支持按操作路由与回退。引用处理和恢复默认值的规则详见[设置指南](web-interface.md)。

凭证仍保存在服务端环境变量中；表单只填写变量名，不填写原始 API Key。配置检查会校验路由与凭证是否可用，但不会真正发送模型请求。检查已保存配置前请先保存。运行中的项目须先暂停再编辑；新建或恢复的任务会捕获已保存设置。

在 **翻译总览** 展开 **完整流程**，可查看 **当前翻译流程**。它按最近一次非导出任务的配置快照展示启用/禁用步骤，并区分书籍、字幕、准备和审校计划。首次任务前显示项目已配置的翻译计划。步骤卡片描述计划本身，不是逐项完成检查点；润色仍在翻译批次内执行。最近一次进度回调在 Redis 中缓存七天并关联 run ID，刷新页面可恢复进度且不会显示旧运行输出。导出任务仍在导出页查看。

## 相关说明

- [界面操作指南](web-interface.md)
- [界面语言](web-i18n.md)
- [WebUI / `dev` 同步说明](sync-dev-webui.md)
- [验收记录](validation.md)
