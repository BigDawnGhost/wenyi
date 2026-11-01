# CLI 使用指南

[English](../cli.md) · [项目介绍](README.md)

Web（React/Vite + FastAPI）部署与开发见 [Web 部署](web.md)。

## 安装与运行

从源码运行需要 Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
export DEEPSEEK_API_KEY=sk-...
uv run wenyi --version
uv run wenyi translate book.epub
```

在仓库根目录执行 `uv sync` 会安装本地 CLI（`wenyi-cli`）和翻译内核（`wenyi-core`）。
命令为 `wenyi`，模块入口为 `python -m wenyi_cli`。开发包含 Web API 的完整 Python
工作区时，使用 `uv sync --locked --all-packages --group dev`。

从使用 `trans-novel` 的旧版工作区升级时，执行 `uv sync --locked` 刷新已安装命令，
之后使用 `uv run wenyi ...`。安装时不再提供旧命令。

显示的版本号由仓库 Git 标签自动生成：标签构建显示正式版本，开发构建还会包含距标签的提交数与提交哈希。
CLI、Core 与 API 包使用同一版本来源，OpenAPI 读取已安装 API 包的版本。
根虚拟工作区和私有前端包不维护独立发布版本。

每次启动程序都会检查当前目录的 `config.yaml`；文件不存在时会创建一份带注释的默认配置。开始正式翻译前请检查模型配置。

API Key 只从环境变量读取。请在本机进程环境中设置，或从共享工作区／会话之外的私有密钥文件加载到环境；不要把真实密钥写入配置、共享文件或对话。CLI 不会自动加载仓库根目录的 `.env` 文件。下方密钥值均为占位符，不是真实凭据。

## 检查模型路由

```bash
uv run wenyi models list
uv run wenyi models explain --operation review.fix
uv run wenyi models check --for translate
uv run wenyi models ping --for translate
```

前三个命令只做本地预览与密钥检查，不发送请求。`models ping` 会对每个可达模型配置发送一次简短的真实请求，确认端点、密钥与模型名可用，会消耗少量 token。三个档位仍可作为默认入口；在 `llm.routes` 中独立配置操作即可混用模型。旧配置与用量账本的显式转换及预算设置见[配置说明](configuration.md#模型与操作路由)。

## 多语言互译（实验性）

先运行 `uv run wenyi languages` 查看内置语言。将以下片段写入自己的配置文件（模型配置沿用已有设置），即可直接中译英：

```yaml
language:
  source: zh
  target: en
```

```bash
uv run wenyi --config config.yaml translate book.epub --bilingual
```

输出为 `output/book.en.epub` 和 `output/book.en-bi.epub`。日译英改为 `source: ja`、`target: en`；英译日用 `source: en`、`target: ja`。反向翻译以对应语言的文件为输入，每次选择一个方向。`--out` 仍遵循显式命名及 `-bi` 派生规则。

以下 `.zh.*` 示例指默认简体中文目标。所有目标的书籍状态均位于 `state/<书名>/targets/<目标语言>/`，字幕位于 `state/srt/<slug>/targets/<目标语言>/`。续跑和独立阶段命令使用同一目标配置，完成的译文不会因切换回原目标而重新翻译。EPUB 说明页在简体中文目标下为中文，其它目标暂用标记为英语的英文页，可通过 `about_page: false` 关闭。

CLI 的帮助、进度、表格和错误提示统一使用英语。译文和模型生成的说明性元数据使用 `language.target`；`source` 和 `aliases` 中的原文姓名保留用于匹配。续跑复用已有分析和术语备注，本次更新不会自动翻译旧数据。当前不支持阿拉伯语、希伯来语等 RTL 目标；PDF 字体和外部 bridge 的语言支持仍需单独验证。真实长篇翻译质量尚未完成新旧盲评。

## Windows

CLI 发行文件名包含 Git tag 版本、组件、平台和架构。以下示例使用 tag `v1.2.3`，
请将 `1.2.3` 替换为下载版本。Desktop 安装包使用独立的 `wenyi-desktop-*` 名称，
详见 [Desktop](desktop.md)。

Windows Release 提供 `wenyi-cli-1.2.3-windows-x64.zip`，请使用
`wenyi-cli-SHA256SUMS.txt` 校验文件，再将 `wenyi.exe` 解压到目录中运行。

使用打包版 `wenyi.exe` 时，在 PowerShell 中设置 API Key：

```powershell
# 仅当前窗口有效
$env:DEEPSEEK_API_KEY = "sk-..."
.\wenyi.exe translate .\book.epub
```

要永久保存环境变量，执行下列命令后重新打开 PowerShell：

```powershell
setx DEEPSEEK_API_KEY "sk-..."
```

也可把 `language.source` 设为已知的语言代码，避免调用模型自动识别源语言。

## Linux

Release 提供 `wenyi-cli-1.2.3-linux-x64.zip` 和 `wenyi-cli-1.2.3-linux-arm64.zip`。
请下载与处理器架构匹配的压缩包，使用 `wenyi-cli-SHA256SUMS.txt` 校验后执行：

```bash
unzip wenyi-cli-1.2.3-linux-arm64.zip  # x64 系统请改用 linux-x64
chmod +x wenyi
export DEEPSEEK_API_KEY=sk-...
./wenyi translate book.epub
```

## macOS

Release 分别提供适用于 Apple Silicon 的 `wenyi-cli-1.2.3-macos-arm64.zip` 和适用于
Intel Mac 的 `wenyi-cli-1.2.3-macos-x64.zip` 终端程序。下载与处理器匹配的压缩包，
先用 `wenyi-cli-SHA256SUMS.txt` 核对文件，再执行：

```bash
unzip wenyi-cli-1.2.3-macos-arm64.zip  # Intel Mac 请改用 macos-x64
chmod +x wenyi
export DEEPSEEK_API_KEY=sk-...
./wenyi translate book.epub
```

这些命令行程序由 PyInstaller 做 ad-hoc 签名，但没有使用 Apple 开发者证书完成
notarization。macOS 仍可能隔离下载的程序；确认校验和无误后，如系统提示拦截，
可在 **系统设置 → 隐私与安全性** 中批准运行。

## 输入与输出

- 输入格式：EPUB、FB2、TXT、Markdown、HTML、PDF、DOCX、SRT。
- Markdown 输入中，以 `#` 至 `######` 开头并与标题文字以空白分隔的标题行会划分为章节。导入时会记录原始标题层级；带页码的普通目录列表仍作为正文保留。
- Markdown 标题层级会保留在 EPUB 正文标题、自动生成的 EPUB 导航目录和 DOCX 大纲样式中。导航将标题嵌套到前面最近且等级数字更小的标题下；跳级不会补造目录条目。已有状态若章节划分正确且保存了标题等级，只需重新导出，无需重新翻译。
- 书籍默认输出：源文件旁 `output/` 下的单语版 `<书名>.zh.epub`（`.docx` 输入默认为 `<书名>.zh.docx`，BabelDOC PDF 状态默认为 `<书名>.zh.pdf`）；双语版 `*.zh-bi.*` 按需开启。
- `--format epub|txt|html|markdown|pdf|docx`：书籍导出格式；`pdf` 仅适用于已保存的 BabelDOC 项目。未指定时 BabelDOC PDF 状态→`pdf`，`.docx`→`docx`，其它书籍（含 MinerU PDF 状态）→`epub`。显式指定的受支持格式优先；PDF 是否可用及默认格式均依据已保存的后端信息，即使当前 `pdf_backend` 配置改变也不会改用另一套规则。该选项不适用于 SRT。
- EPUB 输入会尽量按原 XHTML 模板回填译文，保留样式、图片、目录和锚点。
- 双语版按段展示译文与原文。EPUB/HTML 原文默认淡化，设置 `output.bilingual_preserve_source_style: true` 可改为继承书籍正文样式。DOCX 原文默认使用正常正文样式，不再额外添加灰字、高亮或缩小字号；开启同一选项后，正文和表格会恢复已记录的原文字符样式，包括段内混合样式。排列顺序由 `output.bilingual_order` 独立控制。已有翻译状态只需重新导出。
- EPUB 中未翻译的段落只保留一份原文及其注音、链接。Web 导出和部分已译章节同样遵循此规则，双语排列顺序与原文样式选项不会额外生成重复段落。
- EPUB 默认在书末附加“关于此翻译”说明，可通过 `output.about_page: false` 关闭。
- 书籍状态位于 `state/<书名>/targets/<目标语言>/`，含章节中间结果、术语 SQLite 库、用量和报告。字幕运行使用独立目录树 `state/srt/`（见 [SRT 字幕](#srt-字幕)）。

### 实验性 PDF 支持

PDF 输入和 PDF 导出目前均属于实验性支持。

#### PDF 输入

默认走 MinerU。也可用外部 **BabelDOC bridge**（AGPL，独立仓库/进程，HTTP only）保留版式：

1. 另仓安装并启动 `wenyi-babeldoc-bridge`（默认 `http://127.0.0.1:8765`）
2. `config.yaml`：

```yaml
pipeline:
  pdf_backend: babeldoc
  babeldoc_bridge_url: http://127.0.0.1:8765
  # babeldoc_pages: "15"   # 可选，1-based
```

3. `uv run wenyi translate book.pdf` 会自动经 bridge `/fillback` 导出 PDF。之后执行 `assemble book.pdf` 也会根据已保存的 BabelDOC 状态默认导出 PDF，无需指定 `--format pdf`；需要其它格式时显式指定 `--format`。
   回填 PDF 默认不绘制 BabelDOC 的版面定位框，也不输出 plain text / title 等角色标签。  
   bridge 会把抽取后的原始 IL 冻结为持久 session 快照；只要保留 session 目录并使用完全
   相同的 Python/BabelDOC 版本，服务重启后可按原 session ID 懒恢复，不会重跑版面识别。
   长时间翻译建议用 `WENYI_BABELDOC_STATE_DIR` 指定持久目录；默认系统临时目录可能在重启
   系统后被清理。主仓不 import babeldoc。章节按 **PDF 内置 TOC（书签）** 断章；段落仍带
   `meta.babeldoc_id` 供回填。无书签时退回单章。

BabelDOC 只适合带可提取文本层的 PDF。选择该后端时，Wenyi 会在请求 bridge 前检查所选页面；
若页面只有扫描图片而没有文本层，会停止并提示改用默认 MinerU，或先进行 OCR。

首次读取 PDF（MinerU）需设置 `MINERU_API_KEY`：

```bash
export MINERU_API_KEY=...
uv run wenyi translate book.pdf
```

MinerU 转换生成的 HTML 会保存到
`state/<书名>/targets/<目标语言>/source/<源文件 SHA-256>/converted.html`。按内容隔离缓存，可避免
初始化中断后把另一份 PDF 的转换结果误用于当前文件。
后续运行会直接复用该文件，也可人工修正后再续跑。

#### PDF 导出

PDF 导出仅支持由 BabelDOC 解析的项目，通过外部 bridge 的 `/fillback`
接口回填已保存的译文并保留原始 PDF 排版，无需安装本地 PDF 渲染依赖或选择引擎：

```bash
uv run wenyi assemble book.pdf --format pdf
```

MinerU 项目及其他书籍输入不再支持导出 PDF，请使用 EPUB、DOCX 或 HTML。
CLI、Web、Desktop 使用同一规则；修改当前解析器设置不会将已有项目转换为 BabelDOC 状态。

## DOCX（Word）

`translate book.docx` 走完整书籍 Orchestrator（术语、润色、审校、`state/<slug>/targets/<目标语言>/` 续跑）。

**结构**

- 段落与标题样式（`Heading 1`–`9` / outline）；一级标题切章。
- 简易表格按单元格重建（首版不支持合并单元格 / 嵌套表）。
- Word 自动编号（`numPr`）按组重建为 List Number / List Bullet（按源 list id 分段重开）。
- 支持不含可选编号部件的文档；普通段落不依赖列表编号定义。
- 目录一类正文已含 `1. 标题` 可见序号的行**不再**套自动编号，避免双重序号。

**样式**

- 保留加粗 / 斜体 / 下划线 / 颜色 / 字号，以及段落对齐与底纹。
- 整段同质：导出直接套用，**不**额外调模型。
- 段内混排：译后对每个有意义的跨度单独定位（仿 EPUB 注释标记）；加粗/颜色等属性从原文 item **继承**。单个跨度失败只比例回退该跨度，不整段作废。
- 仅 font/size 差异不参与对齐（噪音）。
- **已译中文**统一**宋体**（不沿用原文西文字体）；**未翻译原文**与双语原文侧不套宋体。
- 模板 Heading 主题蓝会去掉，除非原文写了显式颜色。

**输出**

- 默认：`output/<stem>.zh.docx`（标题大纲可供 Word 导航窗格）。可用 `--format epub` 等覆盖。

```bash
uv run wenyi translate book.docx
uv run wenyi translate book.docx --bilingual
uv run wenyi translate book.docx --format epub
```

## SRT 字幕

`translate` 会按扩展名自动分流 `.srt`。字幕路径比书籍管线更轻：

- 滑窗 20 条、重叠 10，最多 100 路并发 strong 档调用；
- 无术语库、润色或全书审校；
- `--chapter`、`--polish`、`--review`、`--format` 在不适用时会被忽略或拒绝；
- 默认写出单语 `output/<stem>.zh.srt`；加 `--bilingual` 可生成 `.zh-bi.srt`。

```bash
uv run wenyi translate movie.srt
uv run wenyi translate movie.srt --bilingual
uv run wenyi translate movie.srt --no-mono --bilingual
```

再次对同一源文件执行即可续跑；已缓存的
`state/srt/<slug>/targets/<目标语言>/batches/` 会跳过。目录布局：

```text
state/srt/<slug>/targets/<目标语言>/
  manifest.json    # 源身份、字幕条数、滑窗配置
  cues.jsonl       # 每行一条：index / timestamp / source / target / status
  batches/         # 模型原始批次结果，供续跑
  usage.json       # 跨 resume 累计 token
  timing.json      # 累计执行时长与每次运行用时
  events.jsonl     # 运行事件与 LLM 重试观察
```

字幕路径不会生成 `glossary.db` 或 `reviews/`。包代码在 `trans_novel.srt`
（store + translate），读写分别在 `ingest.srt_reader` 与 `assemble.srt_writer`。

## 用量与事件日志

每个目标目录的 `usage.json` 保存跨续跑累计的 token 用量，`events.jsonl` 追加记录阶段事件与重试。Review 目录另存本次审校用量，其增量只合并到总账一次。

缓存统计分别记录读取（`cache_hit_tokens`）、写入（`cache_write_tokens`）、
普通输入（`uncached_tokens`）和分类未知输入（`cache_unknown_tokens`），四类之和为
`prompt_tokens`。显示比例与 `cache_hit_rate` 统一使用**已报告读取 / 全部输入**，
分母包含未知输入；没有可用读取计数或没有输入时，比例为未知而不是 0%。
CLI 摘要同时显示未知 Token 数。这些是 provider 返回的用量计数，不是费用估算，
也不是物理缓存存储的直接证据。

账本 schema 3 还保留已报告的 5m/1h 写入小计，以及可累加的 `*_reported_calls`
次数，区分字段不可用与明确为零。已知小计可能只覆盖部分调用；TTL 分类是写入的子集，
不额外计入输入总量。原生 OpenAI、DeepSeek、Gemini 按各自适配器的用量契约归一化。
通用 Chat Completions 接口使用包含缓存部分的 `prompt_tokens`、
`prompt_tokens_details.cached_tokens`，以及实际返回的
`prompt_tokens_details.cache_write_tokens` 或 `cache_creation_input_tokens`；
TTL 明细来自 `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`。
计数不足以推导普通输入时，剩余部分保留为未知。普通输入、读取、写入分别计数的原生
API 必须由自身适配器归一化，不能套用 Chat Completions 的包含式总量算法。

schema-2 账本在内存中兼容升级，下一次正常保存用量时持久化，保留归属和 Token 总量。
历史 `cache_miss_tokens` 作为兼容计数保留，但**不会**事后猜分为写入和普通输入，
这部分属于分类未知。历史读取为零不能证明 provider 明确上报了零，因此在新调用提供
读取计数之前，其比例为未知。更早的路由归属升级前账本仍需执行
`wenyi models migrate-usage RUN_DIR`。

该命令在既有运行锁内，仅处理选中的 CLI 目标目录及其 Review 账本。先校验全部账本，
再为每个需要转换的文件在同目录保留原始字节备份
`usage.before-routing-<timestamp>.json`，并逐个原子发布转换结果。中断后重复同一命令：
已转换账本会跳过，已有备份会保留。不会发现或迁移 Web/Desktop 状态。

进度条时钟显示本次工作流的总用时，覆盖解析、等待模型响应、翻译、润色、审校和导出。
切换阶段、章节或审校轮次不会归零；一个阶段完成后，如果后续工作仍在进行，时钟仍继续走动。
并发模型请求按实际经过时间计时，不累加各请求的耗时。

`prepare`、`translate`（包括 `--chapter` 和 SRT）、`review` 或 `assemble` 结束后，CLI 显示
最近一次运行用时和累计执行时长。每个目标目录的 `timing.json` 保存 `total_seconds`，以及
包含运行 ID、操作、起止时间、用时和完成状态的 `runs` 列表。重复执行命令只追加本次实际
执行时长，不计入两次运行之间的停机时间；嵌套流程只计一次。分别启动的命令各自计时，
即使它们有重叠执行的时间。书籍也可通过 `wenyi status book.epub` 查看计时记录；
查看状态和重新生成报告不会增加累计时长。

书籍状态初始化成功或通过身份校验后，异常退出和正常 Ctrl+C 中断也会保存本次用时。
计时使用独立锁和原子写入，不影响 token 用量账本。旧版本没有可恢复的计时历史，
累计从本版本开始；强制杀死进程或状态初始化前的失败无法保存本次用时。

manifest 通过 `source_sha256` 绑定输入内容。同名文件内容不同或状态缺少有效哈希时会拒绝续跑，必须重新建立翻译状态。

## 常用命令

```bash
# Parse and list the source chapters without model calls
uv run wenyi parse book.epub

# 一键完整翻译、只翻指定章节，或只准备而不翻译
uv run wenyi translate book.epub
uv run wenyi translate book.epub --chapter 3
uv run wenyi translate book.epub --format txt
uv run wenyi prepare book.epub
uv run wenyi translate book.pdf
uv run wenyi translate movie.srt

# 覆盖配置中的润色与最终审校开关
uv run wenyi translate book.epub --polish --review
uv run wenyi translate book.epub --no-polish --no-review

# 同时生成单语和双语版 / 仅生成双语版
uv run wenyi translate book.epub --bilingual
uv run wenyi translate book.epub --no-mono --bilingual
```

`parse` 列出从零开始的章节编号、原文标题与段落数，并将完整解析原文保存到所显示目标语言状态目录的 `parsed_document.json`。它不需要 LLM 凭据，不调用模型识别语言，不生成梗概、风格或术语，也不把翻译状态标记为初始化成功。`language.source: auto` 留到预处理时识别。PDF 解析仍需要配置的 MinerU/BabelDOC 服务；SRT 使用独立字幕流程，不经过此书籍命令。

之后对同一源文件和配置运行 `prepare` 或 `translate`。匹配的解析缓存会复用，包括 PDF 转换；内容或解析参数变化会使缓存失效。已有初始化状态会拒绝不同源内容，保留已保存译文、分析和用量。

`prepare` 会解析书籍、识别语言、在启用时生成逐章梗概，再生成风格和人物语气指导，最后在启用时生成全书概览，但不翻译正文，也不抽取术语。术语在翻译过程中从已完成的原译文对中抽取；续跑保留项目已有词条。之后对同一源文件运行 `translate`，即可复用状态继续翻译。

启用 `book_understanding` 后，普通翻译与人物证据模式共用相同准备流程：完整原文的章节梗概、原文样本加梗概的风格分析，以及保留原文身份并参考相关已有术语映射的全书概览。相较此前仅取章首 8,000 字符，长章节首次预扫需要更多请求；重试和切换证据模式会复用有效的准备缓存。详见[准备与缓存行为](pipeline.md)。

## 三选一精翻

仅在 Web 创建书籍项目时选择**三译合润精翻**，之后不在总设置或项目配置中提供切换选项；
更换翻译方式需创建新项目。CLI 执行：

```bash
uv run wenyi translate book.epub --translation-mode best_of_three --polish
```

等价配置为 `pipeline.translation_mode: best_of_three` 与
`pipeline.polish: true`。`--translation-mode standard` 将待译批次恢复为默认流程。
精翻与 `--no-polish` 的组合会被拒绝；该选项不适用于 SRT。
续跑时保留相同模式与配置，以复用精翻检查点。已有译文不会自动重新处理。
三份独立初稿后一次原文优先综合润色、续跑及成本/质量限制见[精翻流程](pipeline.md#三选一精翻)。
正常批次调用模型四次，标准翻译加润色为两次，沿用相同路由与输出限制。
最终综合结果仅进行结构校验，不经过独立准确性评判。
对比中的 `target_before_polish` 是第一份初稿，不是选出的准确稿；不保证质量提升。

### 启用全书术语证据

此功能仅 CLI 可用，属于实验功能，默认 `false`。将此片段合并到配置文件，保留现有模型设置：

```yaml
language:
  source: en
  target: zh
pipeline:
  terminology_context: true
```

```bash
uv run wenyi --config config.yaml models explain --operation terminology.evidence
uv run wenyi --config config.yaml models check --for translate
uv run wenyi --config config.yaml prepare book.epub
uv run wenyi --config config.yaml translate book.epub
```

`terminology_context` 仅对译前发现和译后补漏候选中的人物条目（`type: person`），检查全书所有匹配原文组的证据，并向翻译、润色、Review 和 Fixer 请求补充有界、相关的只读备注。译前发现仅处理人物；其他类型保留常规译后抽取和冲突处理，不调用全书取证或合并。全量术语映射不变，既有人物译名和受保护的人工字段保留，不同译名建议记录为冲突，不删除已有备注。`prepare` 只分析风格与人物语气，不抽取或建立术语；取证从待译批次开始。

只有 CLI `prepare` 和 `translate` 获得本次操作的证据生产权限。普通 Orchestrator/Runtime 调用方及 API 不能仅靠 YAML 设置为 `true` 生产证据；Web/UI 未适配。Review、assemble 和 report 可以消费已有备注，不生产证据。SRT 不使用此路径。

术语表仍按词条保存单一原文身份、单一主译名。全书备注可以解释上下文和不确定性，但没有真正的义项模型或逐次出现的译名选择。备注基于原文，只是只读补充，不授权添加后文剧情或覆盖权威译名；无法保证模型始终遵守这些约束。

`terminology.discover`、`terminology.evidence` 和 `terminology.merge` 默认使用 `strong`，会增加上下文、调用次数和首批延迟。FakeClient 测试不是质量证明；真实模型的公版长书前后对比仍待执行。详见[流程说明](pipeline.md#可选全书术语证据)。

## 中断与续跑

已完成的批次会写入状态目录。中断后使用同一个源文件执行：

```bash
uv run wenyi translate book.epub
uv run wenyi status book.epub
```

更改润色设置不会自动重跑已经完成的翻译批次。Review 不同：每次执行
`review` 都会全量重审完整译文，并创建新的时间戳审校目录。默认保持只读；使用
`--autofix` 时可以发布最终修订。只有需要从头翻译时
才应使用新的状态目录或清理对应状态。

语言策略内置于源码。策略变更或保存的状态缺少策略身份时，`prepare`/`translate` 会自动重建受影响的分析，再继续待完成工作；重建可能调用模型。已完成译文继续保留，字体等仅影响导出的变更只需重新导出。书籍／字幕行为和开发者诊断见[内置语言策略](configuration.md#内置语言策略)。

章节梗概现完整分块覆盖原文，v2 原文哈希与策略身份会令旧缓存失效，因此升级后首次续跑可能重新生成分析。梗概人物保留原文身份，译名来自当前术语表。开启术语证据后，失败重试复用缓存的发现结果和成功证据组，补齐未完成准入，保留已译正文；不会把局部分组结果作为完整知识发布。

## 独立阶段与术语管理

```bash
uv run wenyi review book.epub
uv run wenyi review book.epub --autofix
uv run wenyi glossary list book.epub
uv run wenyi glossary conflicts book.epub
uv run wenyi glossary resolve book.epub "原文术语" "指定译名"
uv run wenyi report book.epub
uv run wenyi assemble book.epub
```

`review` 会使用最终术语库检查完整译文。原有 Reviewer 提示词先并发检查连续
文本块；候选问题随后可进入有界取证循环，互相矛盾的跨块一致性建议还可获得
终局建议。确认的问题可以生成仅限本次运行的完整单段影子替换；同轮 Fixer 都读取
同一份不可变快照，下一轮全书 Review 不接收旧问题说明，只盲审更新后的影子译文。
默认情况下，这些替换不会写回。使用 `--autofix`（或设置
`pipeline.review_autofix: true`）时，会先叠加折叠后的 `changes`；剩余 issues
基于更新后的译文复用同一个有界 Review Agent Loop，确认项再复用同一个 Review
Fixer。最终完整段落只写入章节 `target`，不会给章节 JSON 增加 Review 历史字段，
也不修改 manifest 和术语库。每次运行会把面向用户的统一 `result.json`、本次模型用量、事件和内部逐轮记录写入
`state/<书名>/targets/<目标语言>/reviews/review-<时间戳>/`。同一份用量增量还会且只会计入一次
本书累计 `usage.json`。Autofix 的完整前后版本链、issue 判定、失败原因和幂等
写回日志保存在 `autofix/index.json`；发布后会刷新注释与 DOCX 样式偏移。
`report.json` 保存简短的 Review 与 Autofix 摘要。

`report` 汇总当前翻译状态和最新 Review 结果，不会修改正文；`assemble` 可在
不重新调用模型的情况下重新导出已有译文。若另一个终端仍在翻译，导出会读取
调用时已经落盘的一致快照，不必等到整本书结束；之后新完成的批次需再次导出才会进入成品。
