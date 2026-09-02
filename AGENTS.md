# Wenyi repository guide for coding agents

本文件适用于整个仓库。开始修改前先阅读与任务直接相关的源码、测试和文档；若子目录以后出现更具体的 `AGENTS.md`，以更深层文件为准。

## 项目定位

Wenyi（包名 `trans-novel`，Python 包 `trans_novel`）是面向长篇文本的多阶段翻译工具。主流程包含输入解析、全书预扫、术语管理、批次翻译、可选润色、全书 Review、可选 Autofix 发布、报告和多格式导出，并以磁盘状态实现断点续跑。

- Python：3.10+；CI 覆盖 3.10 和 3.12。
- 包管理与命令执行：优先使用 `uv`。
- CLI 入口：`uv run trans-novel ...`，实现位于 `trans_novel/cli.py`。
- 默认配置：仓库根目录 `config.yaml`；内置模板位于 `trans_novel/config.py` 的 `_DEFAULT_CONFIG_YAML`。
- 主仓许可证为 MIT。BabelDOC 是独立 AGPL 服务，主仓只能通过 HTTP bridge 与其通信。

## 目录地图

- `trans_novel/cli.py`：Typer CLI、参数校验、用户可见错误和阶段入口。
- `trans_novel/config.py`：Pydantic 配置模型、默认配置文件生成与 YAML 映射。
- `trans_novel/pipeline/`：书籍工作流。
  - `orchestrator.py` 只负责装配服务、步骤路由和锁作用域。
  - `runtime.py` 保存共享运行依赖、LLM 事件/用量/指标逻辑。
  - `preparation.py`、`translation.py`、`annotations.py`、`review_workflow.py`、`review_autofix.py`、`finalization.py` 分别实现领域阶段。
  - `runstore.py` 定义运行状态、原子写入、源文件身份和跨进程锁。
- `trans_novel/agents/`：模型驱动的分析、翻译、润色、注释对齐、审校取证与影子修订。
- `trans_novel/review/`：Review 的证据索引、运行目录与纯数据模型；不是正式译文存储。
- `trans_novel/ingest/`：EPUB、FB2、TXT/Markdown、HTML、PDF、DOCX、SRT 输入解析及共享数据模型。
- `trans_novel/assemble/`：EPUB、TXT、HTML、Markdown、PDF、DOCX、SRT 输出与报告。
- `trans_novel/glossary/`：SQLite 术语表、抽取、冲突与人工裁定。
- `trans_novel/llm/`：统一 LLM 抽象、档位、重试、用量及 provider 实现。
- `trans_novel/srt/`：字幕专用轻量流程和状态；不经过书籍 Orchestrator。
- `trans_novel/pdf_bridge/`：外部 BabelDOC 服务的纯 HTTP 客户端；不得 import `babeldoc` 或 `pdf2zh`。
- `trans_novel/postprocess/`：确定性文本后处理。
- `tests/`：离线单元、集成、断点续跑、架构边界和回归测试。
- `docs/` 与 `docs/zh/`：英文和中文文档，应保持语义同步。
- `.github/workflows/`：测试与 PyInstaller 多平台构建。

`state/`、`output/`、`review-*`、缓存、构建目录、样例书籍和本地 `packages/core/` 缓存不是当前受版本控制的产品源码。除非用户明确要求，不要读取整本私有书籍、改写、移动、删除或提交这些本地数据。

## 架构边界

依赖方向必须保持：

```text
CLI → Orchestrator → Runtime / Preparation / Translation / Annotation /
                      Review / ReviewAutofix / Finalization → agents / ingest /
                      glossary / assemble / RunStore
```

- `pipeline/orchestrator.py` 是薄 façade。不得直接导入 `agents`、`ingest`、`glossary`、`assemble`、`postprocess` 或 `llm`，不得直接调用领域函数，也不得拥有线程池。
- 下层服务不得反向导入 `orchestrator.py`。
- `agents/` 不得依赖 pipeline 编排、状态机或 RunStore；可使用顶层 `review/` 的纯模型。
- 并发属于具体领域服务。结果必须按稳定的原始顺序合并，不得让线程完成顺序改变输出。
- SRT 是平行轻量路径：无全书预扫、术语库、润色或 Review。字幕修改应留在 `trans_novel.srt`、SRT reader/writer 及对应测试中。
- BabelDOC 集成必须保持进程隔离。主仓只发送 HTTP 请求；不要给 MIT 包新增 AGPL Python 依赖。

`tests/test_architecture_boundaries.py` 和 `tests/test_orchestrator_contract.py` 固定了上述契约。涉及 pipeline 装配或依赖关系时必须运行它们。

## 状态与续跑不变量

- `state/<slug>/manifest.json` 是初始化成功的最终标志。初始化顺序必须保持“派生状态先落盘，manifest 最后原子提交”。
- JSON 状态通过同目录临时文件和 `os.replace` 原子写入；不要改回直接覆盖。
- 书籍状态用 `source_sha256` 绑定输入内容。不得按相同文件名静默复用不同内容的状态。
- 保持锁语义：长流程使用书级运行锁；一致状态读写使用短状态锁；事件追加与产物导出分别使用专用锁。
- 已完成批次和章节必须可安全跳过。修改翻译、术语检查点、预扫或 Review 缓存时，必须覆盖中断后续跑场景。
- 导出从一致快照读取，不能观察到 manifest 与章节文件的混合时刻。
- Review 引擎只能修改本次运行内存/目录中的影子译文。只有显式开启的独立 Review Autofix 发布服务可以在先写可恢复索引后更新正式章节 `target`；它不得新增章节历史字段、修改 manifest 或术语库。
- 用量和事件是追加/累计账本。一次 Review 增量只能合并一次，重试与续跑不得重复计费。
- 保留 `Segment`、章节索引、EPUB 注释/锚点、DOCX 样式和 `babeldoc_id` 等稳定身份；不要为方便处理而重新编号或扁平化。

## 配置、密钥与 provider

- API Key 只从环境变量读取。禁止在源码、测试、文档示例或提交中写入真实密钥。
- 新增或修改用户配置时，同步检查：
  1. `trans_novel/config.py` 的模型与 `_DEFAULT_CONFIG_YAML`；
  2. 根目录 `config.yaml` 示例；
  3. `docs/configuration.md` 与 `docs/zh/configuration.md`；
  4. `tests/test_config.py` 和相关 CLI 测试。
- Provider 专属字段留在对应 provider；通用 LLM 抽象不要感知 DeepSeek/OpenAI/Gemini 等私有协议。
- 重试由 `trans_novel/llm/retrying.py` 统一负责，provider SDK 的内置重试应关闭，避免嵌套重试。
- 测试默认使用 `FakeClient` 或 mock，不能调用真实 LLM、MinerU、BabelDOC 或其它网络服务。
- 用户可预期的输入、配置和外部服务错误应转换为明确异常；CLI 应简洁展示且不打印 traceback。

## 输入与输出约束

- EPUB 修改要同时考虑模板回填、TOC、锚点、内部注释链接、图片、双语原文样式和超长段回并。
- PDF 默认使用 BabelDOC（外部 AGPL HTTP bridge）；MinerU 用于扫描件或无文本层页面。纯图片且无文本层时应给出可操作提示，不应假装成功解析。
- DOCX 修改要保留段落/运行级样式、列表、表格、标题、目录和中英文字体策略。
- 输出格式或命名变化要覆盖单语、双语、显式 `--out`、默认输出目录和并发导出快照。
- 标点、术语命中等可确定行为优先实现为纯函数，并使用边界案例单测固定。

## 开发与验证命令

首次安装：

```bash
uv sync --locked --group dev
```

常用验证：

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q tests/test_<area>.py
uv run pytest -q
```

CI 在已同步环境中使用：

```bash
uv run --no-sync pytest -q
```

若沙箱不允许写用户级 uv 缓存，可为单次命令设置仓库外缓存，例如 `UV_CACHE_DIR=/tmp/wenyi-uv-cache`。不要通过修改 `HOME` 绕过权限。

测试选择建议：

- CLI/配置：`test_cli.py`、`test_config.py`
- 输入/输出：`test_ingest.py`、`test_assemble.py`、`test_bilingual.py`、`test_docx.py`、`test_pdf_support.py`
- Pipeline/续跑：`test_preparation.py`、`test_orchestrator.py`、`test_orchestrator_contract.py`
- Review/润色：`test_review_polish.py`、`test_review_agent.py`
- LLM/provider/重试：`test_llm*.py`、`test_usage.py`
- 术语：`test_glossary.py`、`test_glossary_agents.py`
- 字幕：`test_srt.py`
- 注释链接：`test_annotation_aligner.py`
- 依赖边界：`test_architecture_boundaries.py`

修复缺陷时先添加能失败的最小回归测试。测试数据写入 `tempfile` 目录，保持离线、确定、快速；不要依赖仓库根目录中的真实书籍或已有 `state/`。

## 代码与文档风格

- 遵循现有中文领域命名、注释和用户提示；公共代码保持清晰类型提示和简短 docstring。
- Ruff 配置以 `pyproject.toml` 为准：目标 Python 3.10、行宽 100、E/W/F/I 检查。
- 优先小而可审查的改动，避免顺手重排大文件或更改无关行为。
- 用户行为变化必须更新英文与中文文档；README、usage、configuration、pipeline 只更新受影响部分并保持两种语言一致。
- 更改 prompt、术语、上下文、润色或 Review 语义可能影响整本质量。除自动测试外，应说明质量取舍；重大变更按 `CONTRIBUTING.md` 使用至少 50,000 字公版文本做前后比较，但不要自动处理用户私有样例。

## Git 与交付

- 工作区可能包含用户的修改和大量忽略/未跟踪数据。只暂存本任务明确修改的文件；不得清理、覆盖或提交本地书籍、状态、输出和 review 目录。
- 不使用破坏性 Git 命令，不改写远端历史。只有用户明确要求时才提交或推送。
- 提交信息使用 Conventional Commits 风格，例如 `fix(glossary): ...`、`feat(pdf): ...`、`docs: ...`。
- 交付前至少运行受影响测试、Ruff check、Ruff format check 和 `git diff --check`；高风险跨模块修改运行完整测试集。
- 最终说明应列出行为变化、验证结果、未解决风险，以及未纳入的用户本地文件。
