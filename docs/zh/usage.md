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

- 输入格式：EPUB、FB2、TXT、Markdown、HTML、PDF。
- 默认输出：源文件所在目录 `output/` 中的单语版 `<书名>.zh.epub`；双语版 `<书名>.zh-bi.epub` 需按需开启。
- `--format txt|html|markdown|pdf`：改为导出指定格式；所有输入默认仍生成 EPUB。
- EPUB 输入会尽量按原 XHTML 模板回填译文，保留样式、图片、目录和锚点。
- 双语版按段展示译文与原文，原文默认淡化；设置 `output.bilingual_preserve_source_style: true` 可改为继承书籍正文样式。排列顺序由 `output.bilingual_order` 控制。
- EPUB 默认在书末附加“关于此翻译”说明，可通过 `output.about_page: false` 关闭。
- 状态文件位于 `state/`，包含章节中间结果、术语 SQLite 库和报告。

### 实验性 PDF 支持

PDF 输入和 PDF 导出目前均属于实验性支持。

#### PDF 输入

首次读取 PDF 需设置 `MINERU_API_KEY`：

```bash
export MINERU_API_KEY=...
uv run trans-novel translate book.pdf
```

MinerU 转换生成的 HTML 会保存到 `state/<书名>/source/converted.html`。
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

## 常用命令

```bash
# 一键完整翻译、只翻指定章节，或只准备而不翻译
uv run trans-novel translate book.epub
uv run trans-novel translate book.epub --chapter 3
uv run trans-novel translate book.epub --format txt
uv run trans-novel prepare book.epub
uv run trans-novel translate book.pdf

# 覆盖配置中的润色、最终审校与一致性 QA 开关
uv run trans-novel translate book.epub --polish --review --qa
uv run trans-novel translate book.epub --no-polish --no-review --no-qa

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
`review` 都会全量重审完整译文，并创建新的时间戳只读审校目录。只有需要从头翻译时
才应使用新的状态目录或清理对应状态。

## 独立阶段与术语管理

```bash
uv run trans-novel review book.epub
uv run trans-novel glossary list book.epub
uv run trans-novel glossary conflicts book.epub
uv run trans-novel glossary resolve book.epub "原文术语" "指定译名"
uv run trans-novel qa book.epub
uv run trans-novel report book.epub
uv run trans-novel assemble book.epub
```

`review` 会使用最终术语库检查完整译文。原有 Reviewer 提示词先并发检查连续
文本块；候选问题随后可进入有界取证循环，互相矛盾的跨块一致性建议还可获得
终局建议。确认的问题可以生成仅限本次运行的完整单段影子替换；同轮 Fixer 都读取
同一份不可变快照，下一轮全书 Review 不接收旧问题说明，只盲审更新后的影子译文。
这些替换不会写入 manifest、章节 JSON 或术语库。每次运行会把面向用户的统一
`result.json`、本次模型用量、事件和内部逐轮记录写入
`state/<书名>/reviews/review-<时间戳>/`。同一份用量增量还会且只会计入一次
本书累计 `usage.json`；`report.json` 只保存简短的只读审校摘要。

`qa` 和 `report` 默认只汇总问题，不会修改正文；`assemble` 可在不重新调用模型
的情况下重新导出已有译文。
