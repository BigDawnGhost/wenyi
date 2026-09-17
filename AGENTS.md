# Wenyi repository guide for coding agents

本文件适用于整个仓库；更深层的 `AGENTS.md` 在其范围内优先。按任务查阅相关源码、测试和文档，无需预先通读仓库。

## 工作范围与完成条件

- 实现任务完成请求的行为、相关验证和必要文档；设计或审查任务交付相应文档或发现。常规实现选择自行判断，在授权范围内完成修改、验证和修复。
- 本地离线测试使用临时数据，运行、修复本次改动导致的失败和重测无需逐步确认。缺少影响结果的必要信息时询问，继续不依赖答案的工作。
- Skills 按具体工作流选用，仅加载相关材料。规则造成阻塞时指出文件、具体条款及缺少的信息或授权。

## 项目定位

Wenyi 是支持多语言互译、术语、润色、全书 Review 和多格式导出的长篇文本翻译工具。`wenyi-core` 提供共享内核；`wenyi-cli` 在本地运行，使用文件和 SQLite 保存状态；`wenyi-api` 为 Web 提供 FastAPI 接口与 Arq 任务，使用 PostgreSQL 保存状态、Redis 调度任务和传递进度。

- Python：3.10+；CI 覆盖 3.10 和 3.12。
- 包管理与命令执行：优先使用 `uv`。
- CLI：`uv run wenyi ...`（`packages/cli`）；`wenyi_cli/cli.py` 装配应用，`wenyi_cli/commands/` 和 `model_commands.py` 注册命令。
- Web：`apps/web`（React/Vite）；API 与 worker：`apps/api/wenyi_api`。前端使用 pnpm，Node/pnpm 版本以 CI 和 [Web 文档](docs/web.md) 为准。
- CLI/Core/API 的发布版本由仓库 Git 标签通过 `hatch-vcs` 生成，运行时读取已安装包的元数据；根虚拟工作区和私有前端包不维护独立发布版本。包描述统一使用英语。
- 默认配置：仓库根目录 `config.yaml`；内置模板位于 `packages/core/wenyi_core/config.py` 的 `_DEFAULT_CONFIG_YAML`。
- 主仓许可证为 MIT；BabelDOC 是独立 AGPL 服务。

## 按任务查阅

源码路径相对于 `packages/core/wenyi_core/`（CLI 在 `packages/cli/wenyi_cli/`），测试路径相对于 `packages/core/tests/`；按改动选择用例。

| 任务 | 源码入口与职责 | 相关测试入口 |
| --- | --- | --- |
| CLI、配置 | CLI 的 `commands/`、`model_commands.py`；Core 的 `config.py` | `test_cli*.py`、`test_model_commands.py`、`test_config.py` |
| 装配、初始化、状态与账本 | `pipeline/` 的 `orchestrator.py`、`preparation.py`、`runtime.py`、`runstore.py` | `test_preparation.py`、`test_orchestrator*.py` |
| 存储接口与本地适配 | `storage/protocol.py`、`file.py`、`artifacts.py`；`pipeline/runstore.py` | `test_storage_injection.py`、`test_file_storage.py`、`test_file_artifacts.py` |
| 翻译与上下文 | `pipeline/translation.py`、`translation_batch.py`、`context.py`、`title_translation.py`；`agents/` 模型任务 | `test_translator.py`、`test_translation*.py`、`test_title_translation.py` |
| Review、润色、Autofix | `pipeline/review_*.py`、`autofix_*.py`；`agents/review_*.py`；`review/` 模型、证据与运行记录 | `test_review*.py`、`test_routing_resume.py` |
| 输入、导出与报告 | `ingest/`、`assemble/`、`pipeline/finalization.py`、`postprocess/` | `test_ingest.py`、`test_assemble.py`、`test_bilingual.py` |
| EPUB/HTML 注释与 DOCX 样式 | `markup/` 共享标记处理；`pipeline/annotations.py` 对齐；`document_styles/docx.py` 纯样式策略 | `test_annotation_aligner.py`、`test_docx.py` |
| PDF | `ingest/`、`assemble/` 的 PDF 路径；`pdf_bridge/` | `test_pdf_support.py`、`test_babeldoc_bridge_client.py` |
| 术语 | `glossary/` SQLite、抽取与冲突裁定 | `test_glossary*.py` |
| 模型路由与 provider | `llm/`；操作注册 `operations.py`，provider 注册 `registry.py` | `test_llm*.py`、`test_usage.py`、`test_routing_resume.py` |
| 多语言与提示词 | `i18n/`；语言规则和任务模板统一在 `i18n/data/` | `test_i18n.py`、`test_metadata_language.py` |
| 字幕 | `srt/` 及 SRT reader/writer | `test_srt.py` |

Web 相关入口使用仓库根目录相对路径：

- API、任务与数据库：`apps/api/wenyi_api/` 下的 `routers/`、`workers/`、`storage_pg.py`、`db/`；测试在 `apps/api/tests/`。
- 前端页面与进度：`apps/web/src/` 下的 `features/`、`lib/api.ts`、`lib/ws.ts`；E2E 在 `apps/web/tests/`。API 类型在 `packages/shared-schema/`，接口变更需同步检查后端 schema、共享类型与前端调用。

配置查阅 [configuration](docs/configuration.md)，流程语义查阅 [pipeline](docs/pipeline.md)，Web 部署查阅 [web](docs/web.md)，模块职责查阅 [architecture](docs/architecture.md)；中文版在 `docs/zh/`。历史设计稿不代表当前行为。CI 与打包查阅 `.github/workflows/` 和 `pyproject.toml`。

`packages/core/`、`packages/cli/` 和 `apps/` 包含产品源码；其中忽略的运行产物仍属于本地数据。`state/`、`output/`、`review-*`、Web `DATA_DIR`、缓存、构建目录和样例书籍均需保留。除非用户明确要求，不读取整本私有书籍，不改写、移动、删除或提交这些数据。

## 架构边界

依赖方向必须保持：

```text
CLI / Web worker → Orchestrator → Runtime / Preparation / Translation / Annotation /
                                  Review / ReviewAutofix / Finalization
                               → agents / ingest / glossary / assemble / Storage 接口
```

- `wenyi_core` 不依赖 CLI、Web API 或其框架。领域服务通过 `Storage` / `ArtifactStorage` 读写运行状态；本地适配器组合 RunStore 与 SQLite，Web 注入 `PostgresStorage`，不得在领域服务中绕过接口直接写文件状态或打开 SQLite。
- `pipeline/orchestrator.py` 是薄 façade。不得直接导入 `agents`、`ingest`、`glossary`、`assemble`、`postprocess` 或 `llm`，不得直接调用领域函数，也不得拥有线程池。
- 下层服务不得反向导入 `orchestrator.py`。
- `cli.py` 保持应用装配职责；命令通过显式注册和调用上下文接收依赖，不反向导入 CLI 全局对象。
- `agents/` 不得依赖 pipeline 编排、状态机或 RunStore；可使用顶层 `review/` 的纯模型。
- `markup/` 和 `document_styles/` 不依赖 pipeline、LLM 或具体 writer。拆分时分离决策、执行与状态写入，不通过共享整个 runtime 延续耦合。
- 并发属于具体领域服务。结果必须按稳定的原始顺序合并，不得让线程完成顺序改变输出。
- SRT 独立于书籍 Orchestrator，无全书预扫、术语库、润色或 Review。
- BabelDOC 集成必须保持进程隔离。主仓只发送 HTTP 请求；不要给 MIT 包新增 AGPL Python 依赖。

涉及模块依赖关系时运行 `packages/core/tests/test_architecture_boundaries.py`；涉及 pipeline 装配或依赖关系时同时运行 `packages/core/tests/test_orchestrator_contract.py`。存储适配变更还需运行 `test_storage_injection.py` 和对应后端测试。

## 状态与续跑不变量

- CLI 书籍状态按 `state/<slug>/targets/<target-language>/` 隔离；派生状态先落盘，`manifest.json` 最后原子提交，作为初始化成功标志。文件 JSON 通过同目录临时文件和 `os.replace` 原子写入。
- Web 可变状态由 PostgreSQL 按项目持久化，通过事务与 advisory lock 保持一致性；`DATA_DIR` 仅保存上传原文、解析缓存和导出产物，不另存 JSON/SQLite 状态副本。
- Artifact key 使用运行目录相对路径和 `/` 分隔符，在所有平台及后端保持一致；按前缀检索时限制扫描范围，避免遍历无关源文缓存。
- `source_sha256` 绑定输入内容，不按相同文件名复用不同内容的状态。
- 保持锁语义：长流程使用书级运行锁；一致状态读写使用短状态锁；事件追加与产物导出分别使用专用锁。
- 已完成批次和章节必须可安全跳过。修改翻译、术语检查点、预扫或 Review 缓存时，必须覆盖中断后续跑场景。
- `target is None` 表示待翻译；MinerU 路径允许有意保存空译文 `""`，不能用真假判断将其视为未完成。
- 导出从 manifest 与章节数据的一致快照读取。
- Review 引擎只能修改本次运行内存/目录中的影子译文。只有显式开启的独立 Review Autofix 发布服务可以在先写可恢复索引后更新正式章节 `target`；它不得新增章节历史字段、修改 manifest 或术语库。
- 本地独立 Review 使用术语库只读快照，包含已提交的 WAL 数据且不触碰正式数据库文件；文件存储拥有的术语连接在运行锁退出时释放，包括异常退出。
- 用量和事件是追加/累计账本，Review 增量只合并一次，重试与续跑不得重复计费。
- 保留 `Segment`、章节索引、EPUB 注释/锚点、DOCX 样式和 `babeldoc_id` 等稳定身份。

## 配置、密钥与 provider

- API Key 只从环境变量读取。禁止在源码、测试、文档示例或提交中写入真实密钥。
- 配置变更同步模型、默认模板、根目录 `config.yaml`、中英文 configuration 文档及配置/CLI 测试；LLM 配置模型位于 `llm/configuration.py`。
- 新模型操作和 provider 使用现有注册入口，保持操作 ID、路由预览、校验与实际执行一致。
- Provider 专属字段留在对应 provider，通用 LLM 抽象不感知私有协议。
- 重试由 `packages/core/wenyi_core/llm/retrying.py` 统一负责，provider SDK 的内置重试应关闭，避免嵌套重试。
- 用户可预期的输入、配置和外部服务错误应转换为明确异常；CLI 应简洁展示且不打印 traceback。

## 输入与输出约束

- EPUB 修改要同时考虑模板回填、TOC、锚点、内部注释链接、图片、双语原文样式和超长段回并。
- PDF 默认使用 MinerU；BabelDOC（外部 AGPL HTTP bridge）用于尽量保留版式的可选路径。纯图片且无文本层时应给出可操作提示，不应假装成功解析。
- DOCX 修改要保留段落/运行级样式、列表、表格、标题、目录和中英文字体策略。
- 输出格式或命名变化要覆盖单语、双语、显式 `--out`、默认输出目录和并发导出快照。
- 标点、术语命中等可确定行为优先实现为纯函数，并使用边界案例单测固定。

## 验证与完成

验证范围取决于行为风险：

- Python 变更运行受影响测试、Ruff check/format check 和 `git diff --check`；状态、锁、续跑及跨领域的高风险变更运行完整测试集。依赖检查按上面的触发条件执行。
- 前端变更运行 typecheck、build 和受影响的 Playwright E2E；接口、鉴权和进度事件变更同时检查 API 与前端。测试断言当前可见行为，并匹配真实事件的项目及运行身份。
- 纯文档或注释变更检查差异、路径、命令和适用格式，无需全套测试。验证通过后，有新改动、失败或未解决疑点再扩大或重复检查。
- 缺陷修复先添加最小失败回归用例；行为不变的重构复用现有测试，按覆盖缺口补充。模型和解析服务测试使用 `FakeClient` 或 mock 与临时目录，不调用真实 LLM、MinerU、BabelDOC 等外部服务，不依赖本地书籍与状态。
- PostgreSQL/Redis 集成测试仅使用隔离测试服务，通过 `WENYI_TEST_DATABASE_URL`、`WENYI_TEST_REDIS_URL` 启用；缺少服务而跳过的用例须在交付中说明，不能算作后端验证通过。
- 涉及 CLI/Core 安装、入口或打包的变更，在干净环境验证默认安装，并构建 sdist 和 wheel、检查版本及提示词资源；目录迁移后确认架构测试扫描到实际文件，避免空循环通过。CI 中有对应安装与资源检查入口。
- prompt、术语、上下文、润色或 Review 语义变更说明质量取舍；重大变更按 [CONTRIBUTING.md](CONTRIBUTING.md) 使用公版文本做前后比较。离线测试不能代替模型质量评估，未执行时说明限制。

默认 `uv sync --locked` 安装本地 CLI 与 Core。完整 Python 工作区开发使用 `uv sync --locked --all-packages --group dev`；之后用 `--no-sync` 保留已安装的 API 等工作区依赖。以下命令从仓库根目录执行：

```bash
uv run --no-sync ruff check packages/core packages/cli apps/api
uv run --no-sync ruff format --check packages/core packages/cli apps/api
uv run --no-sync pytest -q packages/core/tests/test_config.py
uv run --no-sync pytest -q
git diff --check
```

前端安装使用 `pnpm install --frozen-lockfile`，验证使用 `pnpm -C apps/web typecheck`、`pnpm -C apps/web build` 和 `pnpm -C apps/web test:e2e`；E2E 需要 Playwright Chromium，安装与服务配置见 [Web 文档](docs/web.md) 和 CI。

若沙箱不允许写用户级 uv 缓存，为单次命令设置 `UV_CACHE_DIR=/tmp/wenyi-uv-cache`，不要修改 `HOME`。

## 代码与文档风格

- 代码注释、docstring、配置注释和默认 CLI 文案统一使用标准英语；公共代码保持清晰类型提示。提示词指令使用英语，模型生成的说明性内容使用目标语言，原文身份字段与语言示例保留原样。
- Ruff 配置以 `pyproject.toml` 为准：目标 Python 3.10、行宽 100、E/W/F/I 检查。
- 优先小而可审查的改动，避免顺手重排大文件或更改无关行为。
- 用户行为变化必须更新英文与中文文档；README、usage、configuration、pipeline 只更新受影响部分并保持两种语言一致。

## Git 与交付

- 保留用户已有修改及忽略/未跟踪数据，只暂存本任务明确修改的文件。
- 不使用破坏性 Git 命令，不改写远端历史。提交或推送需要用户明确要求；同一任务内已授权的操作无需重复确认。
- 提交信息使用 Conventional Commits 风格，例如 `fix(glossary): ...`、`feat(pdf): ...`、`docs: ...`。
- 交付说明行为变化、实际验证结果、未解决风险和未执行的必要验证，以及未纳入的用户本地文件。

## 维护本指南

只保留项目事实、约束、任务入口与完成标准；删除过时或重复规则，专门流程按需链接。参考 [OpenAI 的 AGENTS.md 与 skills 建议](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)，不绑定模型或固定工具调用顺序。
