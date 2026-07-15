# 使用指南

[English](../usage.md)

## 安装与运行

从源码运行需要 Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
export DEEPSEEK_API_KEY=sk-...
uv run trans-novel translate book.epub
```

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

## 输入与输出

- 输入格式：EPUB、FB2、TXT、Markdown、HTML、PDF。
- 默认输出：源文件所在目录 `output/` 中的单语版 `<书名>.zh.epub`；双语版 `<书名>.zh-bi.epub` 需按需开启。
- `--format txt|html|markdown`：改为导出指定格式；所有输入默认仍生成 EPUB。
- PDF 首次读取需设置 `MINERU_API_KEY`。转换结果保存为 `state/<书名>/source/converted.html`，后续运行会直接复用，也可人工修正后再续跑。
- EPUB 输入会尽量按原 XHTML 模板回填译文，保留样式、图片、目录和锚点。
- 双语版按段展示译文与淡化原文，排列顺序由 `output.bilingual_order` 控制。
- EPUB 默认在书末附加“关于此翻译”说明，可通过 `output.about_page: false` 关闭。
- 状态文件位于 `state/`，包含章节中间结果、术语 SQLite 库和报告。

## 常用命令

```bash
# 翻译、只翻指定章节、导出 TXT
uv run trans-novel translate book.epub
uv run trans-novel translate book.epub --chapter 3
uv run trans-novel translate book.epub --format txt
uv run trans-novel translate book.pdf

# 覆盖配置中的润色与审校开关
uv run trans-novel translate book.epub --polish --qa
uv run trans-novel translate book.epub --no-polish --no-qa

# 同时生成单语和双语版 / 仅生成双语版
uv run trans-novel translate book.epub --bilingual
uv run trans-novel translate book.epub --no-mono --bilingual
```

## 中断与续跑

已完成的批次会写入状态目录。中断后使用同一个源文件执行：

```bash
uv run trans-novel resume book.epub
uv run trans-novel status book.epub
```

更改润色或审校开关不会自动重跑已经完成的批次；需要重新翻译时请使用新的状态目录或清理对应状态。

## 常用工具

```bash
uv run trans-novel tools glossary book.epub list
uv run trans-novel tools glossary book.epub conflicts
uv run trans-novel tools qa book.epub
uv run trans-novel tools report book.epub
uv run trans-novel tools assemble book.epub
```

`qa` 和 `report` 默认只汇总问题，不会修改正文；`assemble` 可在不重新调用模型的情况下重新导出已有译文。
