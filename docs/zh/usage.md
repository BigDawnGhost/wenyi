# 使用指南

[English](../usage.md)

## 安装与运行

从源码运行需要 Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
export DEEPSEEK_API_KEY=sk-...
uv run trans-novel --version
uv run trans-novel translate book.epub
```

显示的版本号由仓库 Git 标签自动生成：标签构建显示正式版本，开发构建还会包含距标签的提交数与提交哈希。

每次启动程序都会检查当前目录的 `config.yaml`；文件不存在时会创建一份带注释的默认配置。开始正式翻译前请检查模型配置。

## Windows

Windows Release 提供 `wenyi-windows-x64.zip`，运行前请使用
`SHA256SUMS.txt` 校验文件。

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

Release 提供 `wenyi-linux-x64.tar.gz` 和 `wenyi-linux-arm64.tar.gz`。请下载与
处理器架构匹配的压缩包，使用 `SHA256SUMS.txt` 校验后执行：

```bash
tar -xzf wenyi-linux-arm64.tar.gz  # x64 系统请改用 wenyi-linux-x64.tar.gz
chmod +x wenyi
export DEEPSEEK_API_KEY=sk-...
./wenyi translate book.epub
```

## macOS

Release 分别提供适用于 Apple Silicon 的 `wenyi-macos-arm64.tar.gz` 和适用于
Intel Mac 的 `wenyi-macos-x64.tar.gz` 终端程序。下载与处理器匹配的压缩包，先用
`SHA256SUMS.txt` 核对文件，再执行：

```bash
tar -xzf wenyi-macos-arm64.tar.gz  # Intel Mac 请改用 wenyi-macos-x64.tar.gz
chmod +x wenyi
export DEEPSEEK_API_KEY=sk-...
./wenyi translate book.epub
```

这些命令行程序由 PyInstaller 做 ad-hoc 签名，但没有使用 Apple 开发者证书完成
notarization。macOS 仍可能隔离下载的程序；确认校验和无误后，如系统提示拦截，
可在 **系统设置 → 隐私与安全性** 中批准运行。

## 输入与输出

- 输入格式：EPUB、FB2、TXT、Markdown、HTML、PDF、DOCX、SRT。
- 书籍默认输出：源文件旁 `output/` 下的单语版 `<书名>.zh.epub`（`.docx` 输入默认改为 `<书名>.zh.docx`）；双语版 `*.zh-bi.*` 按需开启。
- `--format epub|txt|html|markdown|pdf|docx`：书籍导出格式；未指定时 `.docx`→`docx`，其它书籍→`epub`。该选项不适用于 SRT。
- EPUB 输入会尽量按原 XHTML 模板回填译文，保留样式、图片、目录和锚点。
- 双语版按段展示译文与原文，原文默认淡化；设置 `output.bilingual_preserve_source_style: true` 可改为继承书籍正文样式。排列顺序由 `output.bilingual_order` 控制。
- EPUB 默认在书末附加“关于此翻译”说明，可通过 `output.about_page: false` 关闭。
- 书籍状态位于 `state/<书名>/`，含章节中间结果、术语 SQLite 库、用量和报告。字幕运行使用独立目录树 `state/srt/`（见 [SRT 字幕](#srt-字幕)）。

### 实验性 PDF 支持

PDF 输入和 PDF 导出目前均属于实验性支持。

#### PDF 输入

默认走 BabelDOC，经外部 **BabelDOC bridge**（AGPL，独立仓库/进程，HTTP only）保留版式。
扫描件、无文本层页面请改用 MinerU。

1. 另仓安装并启动 `wenyi-babeldoc-bridge`（默认 `http://127.0.0.1:8765`）
2. 默认 `config.yaml` 已选择 BabelDOC：

```yaml
pipeline:
  pdf_backend: babeldoc
  babeldoc_bridge_url: http://127.0.0.1:8765
  # babeldoc_pages: "15"   # 可选，1-based
```

改用 MinerU：

```yaml
pipeline:
  pdf_backend: mineru
```

3. `uv run trans-novel translate book.pdf` 后 `assemble --format pdf` 会经 bridge `/fillback` 出 PDF。  
   bridge 会把抽取后的原始 IL 冻结为持久 session 快照；只要保留 session 目录并使用完全
   相同的 Python/BabelDOC 版本，服务重启后可按原 session ID 懒恢复，不会重跑版面识别。
   长时间翻译建议用 `WENYI_BABELDOC_STATE_DIR` 指定持久目录；默认系统临时目录可能在重启
   系统后被清理。主仓不 import babeldoc。章节按 **PDF 内置 TOC（书签）** 断章；段落仍带
   `meta.babeldoc_id` 供回填。无书签时退回单章。

BabelDOC 只适合带可提取文本层的 PDF。选择该后端时，Wenyi 会在请求 bridge 前检查所选页面；
若页面只有扫描图片而没有文本层，会停止并提示改用 MinerU（`pipeline.pdf_backend: mineru`），或先进行 OCR。

首次读取 PDF（MinerU）需设置 `MINERU_API_KEY`：

```bash
export MINERU_API_KEY=...
uv run trans-novel translate book.pdf
```

MinerU 转换生成的 HTML 会保存到
`state/<书名>/source/<源文件 SHA-256>/converted.html`。按内容隔离缓存，可避免
初始化中断后把另一份 PDF 的转换结果误用于当前文件。
后续运行会直接复用该文件，也可人工修正后再续跑。

#### PDF 导出

默认 PDF 引擎为 WeasyPrint。安装对应的可选依赖后，无需指定
`--pdf-engine`：

```bash
uv sync --extra pdf-output
uv run trans-novel assemble book.html --format pdf
```

如需不依赖系统排版库的跨平台轻量引擎，可使用 `fpdf2`：

```bash
uv sync --extra pdf-output-lite
uv run trans-novel assemble book.html --format pdf --pdf-engine fpdf2
```

`fpdf2` 可处理基础排版和图片，但只支持有限的 HTML/CSS；与文字混排的图片
会作为独立区块输出。它会查找系统中的中文字体；如果未找到，请用
`TRANS_NOVEL_PDF_FONT` 指定 TTF、OTF 或 TTC 字体文件。此方案也适用于
Windows。

## DOCX（Word）

`translate book.docx` 走完整书籍 Orchestrator（术语、润色、审校、`state/<slug>/` 续跑）。

**结构**

- 段落与标题样式（`Heading 1`–`9` / outline）；一级标题切章。
- 简易表格按单元格重建（首版不支持合并单元格 / 嵌套表）。
- Word 自动编号（`numPr`）按组重建为 List Number / List Bullet（按源 list id 分段重开）。
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
uv run trans-novel translate book.docx
uv run trans-novel translate book.docx --bilingual
uv run trans-novel translate book.docx --format epub
```

## SRT 字幕

`translate` 会按扩展名自动分流 `.srt`。字幕路径比书籍管线更轻：

- 滑窗 20 条、重叠 10，最多 100 路并发 strong 档调用；
- 无术语库、润色或全书审校；
- `--chapter`、`--polish`、`--review`、`--format` 在不适用时会被忽略或拒绝；
- 默认写出单语 `output/<stem>.zh.srt`；加 `--bilingual` 可生成 `.zh-bi.srt`。

```bash
uv run trans-novel translate movie.srt
uv run trans-novel translate movie.srt --bilingual
uv run trans-novel translate movie.srt --no-mono --bilingual
```

再次对同一源文件执行即可续跑；已缓存的
`state/srt/<slug>/batches/` 会跳过。目录布局：

```text
state/srt/<slug>/
  manifest.json    # 源身份、字幕条数、滑窗配置
  cues.jsonl       # 每行一条：index / timestamp / source / target / status
  batches/         # 模型原始批次结果，供续跑
  usage.json       # 跨 resume 累计 token
  events.jsonl     # 运行事件与 LLM 重试观察
```

字幕路径不会生成 `glossary.db` 或 `reviews/`。包代码在 `trans_novel.srt`
（store + translate），读写分别在 `ingest.srt_reader` 与 `assemble.srt_writer`。

## 单次运行指标

`state/<书名>/usage.json` 继续保存这本书跨续跑累计的 token 总账。`translate`、
`prepare`、`review`、`assemble` 和 `report` 会各自生成一份
`state/<书名>/run_metrics/<run-id>.json`，记录：

- 输入文件 SHA-256、配置、程序包和 Git 提交的指纹；
- 指定章节、输出格式、PDF 引擎等本次调用参数；
- 本次请求的阶段、成功或失败状态，以及各阶段墙钟耗时；
- 仅由本次命令新增的模型调用数与 token；
- 命令结束时已完成的章节数和正文段数。

每次续跑都会新建一条记录，因此不同分支的全新运行可以公平比较，不会把历史
成本混在一起。账本不保存完整源文件路径或书籍正文；敏感配置值会被遮蔽，失败
时也只记录异常类型。

新 manifest 使用 `source_sha256`，不再保存源文件绝对路径。若同名状态目录记录的
哈希与当前输入不一致，Wenyi 会拒绝续跑；旧版本生成的 manifest 需要重新建立。

## 常用命令

```bash
# 一键完整翻译、只翻指定章节，或只准备而不翻译
uv run trans-novel translate book.epub
uv run trans-novel translate book.epub --chapter 3
uv run trans-novel translate book.epub --format txt
uv run trans-novel prepare book.epub
uv run trans-novel translate book.pdf
uv run trans-novel translate movie.srt

# 覆盖配置中的润色与最终审校开关
uv run trans-novel translate book.epub --polish --review
uv run trans-novel translate book.epub --no-polish --no-review

# 同时生成单语和双语版 / 仅生成双语版
uv run trans-novel translate book.epub --bilingual
uv run trans-novel translate book.epub --no-mono --bilingual
```

`prepare` 会解析书籍、识别语言、生成风格指南和初始术语表，并完成配置中启用的全书预扫，但不翻译任何正文。之后对同一源文件运行 `translate`，即可复用状态继续翻译。

## 中断与续跑

已完成的批次会写入状态目录。中断后使用同一个源文件执行：

```bash
uv run trans-novel translate book.epub
uv run trans-novel status book.epub
```

更改润色设置不会自动重跑已经完成的翻译批次。Review 不同：每次执行
`review` 都会全量重审完整译文，并创建新的时间戳审校目录。默认保持只读；使用
`--autofix` 时可以发布最终修订。只有需要从头翻译时
才应使用新的状态目录或清理对应状态。

## 独立阶段与术语管理

```bash
uv run trans-novel review book.epub
uv run trans-novel review book.epub --autofix
uv run trans-novel glossary list book.epub
uv run trans-novel glossary conflicts book.epub
uv run trans-novel glossary resolve book.epub "原文术语" "指定译名"
uv run trans-novel report book.epub
uv run trans-novel assemble book.epub
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
`state/<书名>/reviews/review-<时间戳>/`。同一份用量增量还会且只会计入一次
本书累计 `usage.json`。Autofix 的完整前后版本链、issue 判定、失败原因和幂等
写回日志保存在 `autofix/index.json`；发布后会刷新注释与 DOCX 样式偏移。
`report.json` 保存简短的 Review 与 Autofix 摘要。

`report` 汇总当前翻译状态和最新 Review 结果，不会修改正文；`assemble` 可在
不重新调用模型的情况下重新导出已有译文。若另一个终端仍在翻译，导出会读取
调用时已经落盘的一致快照，不必等到整本书结束；之后新完成的批次需再次导出才会进入成品。
