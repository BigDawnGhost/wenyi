# 文译 · Wenyi

面向长篇书籍与字幕的多语言翻译工具，同时提供 Web UI 和本地 CLI。

[English](../../README.md) | **简体中文**

本分支沿用 `webui` 的 monorepo 架构，同步 `dev` 已实现的领域功能。默认书籍流程包括全书预理解、翻译、润色、全书审校和自动修复。

![双语 EPUB 预览](../images/bilingual-preview.png)

## 主要能力

- **多语言互译**：自动检测源语言，支持内置语言及简繁中文、英美英语、葡萄牙语等变体；完整列表见 `trans-novel languages`。
- **长篇上下文**：全书梗概、章节摘要、滚动上下文、按 Token 预算切分批次；支持在翻译会话内继续润色、实时术语提取和章节标题翻译。
- **全书审校**：证据核查、冲突仲裁、影子译文修订与独立 Autofix 发布。已完成且输入一致的审校可复用；中断审校可恢复检查点。
- **格式支持**：EPUB、DOCX、FB2、TXT、Markdown、HTML、PDF 书籍流程，以及独立的 SRT 字幕翻译、编辑和双语导出。
- **模型配置**：提供商连接、模型配置、`strong` / `cheap` / `fast` 档位、按操作路由、显式备用模型、并发限制、请求/Token 预算、按模型和提供商统计用量。
- **Web 工作区**：项目配置、实时进度、术语、人工校阅、风格、审校历史、字幕编辑和下载。独立导出队列可在翻译期间读取已保存的一致快照。

## Web 部署

```bash
git clone --branch webui https://github.com/BigDawnGhost/wenyi.git
cd wenyi
cp .env.example deploy/.env
# 在 deploy/.env 填入 config.yaml 所选提供商对应的凭证。
docker compose -f deploy/docker-compose.yml --profile full up --build
```

打开 [Web UI](http://localhost:8080) 或 [API 文档](http://localhost:8000/docs)。`server` profile 启动 API、两个 Worker、PostgreSQL 和 Redis，不启动前端。

API 与 Worker 共享配置文件、环境变量凭证和 `/data` 数据卷。Web 可变状态存入 PostgreSQL；数据卷保存上传原件、解析资源和导出文件。详见 [Web 部署与开发](../web.md)。

## 本地 CLI

需要 Python 3.10+ 和 `uv`，无需数据库或 Redis。

```bash
uv sync --all-packages
export DEEPSEEK_API_KEY=your-key
uv run trans-novel translate book.epub
```

默认提供商为 DeepSeek。可在 `config.yaml` 切换提供商，并设置对应环境变量。仅需 CLI 时使用 `uv sync --package wenyi-cli`。

```bash
uv run trans-novel prepare book.epub
uv run trans-novel translate book.epub --bilingual
uv run trans-novel review book.epub --no-autofix
uv run trans-novel status book.epub
uv run trans-novel assemble book.epub --format docx
uv run trans-novel translate captions.srt --bilingual
```

**Review 与 Autofix 默认开启。** 仅查看建议时使用 `review --no-autofix`。想降低处理量，可使用 Web 的“快速出稿”或在配置中关闭预理解、润色和审校。重复执行命令会继续保存的进度。

CLI 按目标语言隔离状态：书籍使用 `state/<书名>/targets/<语言>/`，字幕使用 `state/srt/<名称>/targets/<语言>/`。源文件哈希用于防止错误续跑。导出通常位于 `output/<书名>.<语言>.<格式>`，双语版带 `-bi`。

## 格式与可选服务

| 输入 | 处理说明 |
|---|---|
| EPUB / HTML | 保留支持的结构、资源、目录、锚点、Ruby 与注释链接；EPUB 支持逐段注释定位。 |
| DOCX | 保留支持的标题、列表、表格、段落和行内样式，按目标语言选择字体策略。 |
| TXT / Markdown / FB2 | 本地解析为章节与段落。 |
| PDF / MinerU | 默认 PDF 路径，需要 `MINERU_API_KEY`；缓存解析结果，扫描件由 MinerU 处理。 |
| PDF / BabelDOC | 配置选择的独立 HTTP bridge；保存解析后端信息，供 PDF 回填导出使用。 |
| SRT | 独立字幕条目和批次缓存，保留序号与时间戳；不运行书籍预理解或全书审校。 |

本地 PDF 输出可安装 `uv sync --all-packages --extra pdf-output`（WeasyPrint）或 `--extra pdf-output-lite`（fpdf2）。Docker 后端镜像默认包含两者和 Noto 字体；设 `INSTALL_PDF_OUTPUT=false` 可省略 Python PDF 输出依赖。BabelDOC bridge 需要单独部署。

## 架构与升级方式

- `apps/web`：React/Vite；`apps/api`：FastAPI/Arq。
- `wenyi:workflows` 队列处理解析、准备、翻译、审校与模型对比；`wenyi:exports` 队列独立导出。
- `packages/core` 提供领域服务与存储接口；`packages/cli` 提供 `trans-novel`。
- Web 使用 PostgreSQL 存储项目、术语、Review、字幕与账本；CLI 使用文件和 SQLite。Web 不维护数据库状态的本地 JSON/SQLite 副本。
- `packages/shared-schema` 保存由 OpenAPI 生成的前端接口类型。

此次变更面向**全新 Web 部署**，不自动迁移旧数据库、项目策略或状态。保留旧安装时，为新版使用独立数据库和数据卷。

## 文档与验证

[使用指南](usage.md) · [配置指南](configuration.md) · [流水线与存储](pipeline.md) · [Web 部署](../web.md) · [同步说明](../sync-dev-webui.md)

```bash
uv sync --all-packages --group dev
uv run pytest -q
uv run ruff check packages/core packages/cli apps/api
pnpm install --frozen-lockfile
pnpm -C apps/web typecheck
pnpm -C apps/web build
```

设置 `WENYI_TEST_DATABASE_URL` 可运行真实 PostgreSQL 存储及工作流集成测试。CI 提供 PostgreSQL/Redis、Python 3.10/3.12、前端构建与浏览器测试。离线模型替身验证功能和恢复行为，不代表真实模型的译文质量评估结果。

[贡献指南](CONTRIBUTING.md) · [MIT 许可](../../LICENSE)
