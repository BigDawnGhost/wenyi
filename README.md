# 文译

专注于将多语言 EPUB、FB2 或 TXT 小说翻译成中文，并尽量保留 EPUB 原排版、图片、目录和跳转。

项目的日常入口只有一个命令：`translate`。它会完成预扫、分析、翻译、可选润色、章末审校、标点规范化和 EPUB 导出；中断后可以继续跑。

## 快速开始

```bash
uv sync
export DEEPSEEK_API_KEY=sk-...
uv run trans-novel translate book.epub
```

Windows PowerShell（打包版 `wenyi.exe`）先设置 API Key：

```powershell
# 仅当前 PowerShell 窗口生效
$env:DEEPSEEK_API_KEY = "sk-..."
.\wenyi.exe translate .\book.epub
```

翻译完成后，默认会在源文件目录下生成译文 EPUB。运行状态、章节 JSON、术语库和报告会放在 `state/` 目录下。

中断后继续：

```bash
uv run trans-novel resume book.epub
```

查看进度：

```bash
uv run trans-novel status book.epub
```

仅重新导出 EPUB：

```bash
uv run trans-novel tools assemble book.epub
```

## 输入和输出

- 输入：EPUB、FB2、TXT。
- 默认输出：中文 EPUB。
- EPUB 输入会按原 XHTML 模板回填译文，尽量保留原书样式、图片、目录和锚点。
- TXT 输入会生成新的 EPUB。
- 需要纯文本时使用 `--format txt`。

示例：

```bash
uv run trans-novel translate book.epub
uv run trans-novel translate book.epub --format txt
uv run trans-novel translate book.epub --chapter 3
```

## 常用开关

```bash
uv run trans-novel translate book.epub --polish
uv run trans-novel translate book.epub --no-polish
uv run trans-novel translate book.epub --qa
uv run trans-novel translate book.epub --no-qa
```

`--polish/--no-polish` 会覆盖 `config.yaml` 里的 `pipeline.polish`。当前仓库的 `config.yaml` 写的是 `polish: true`，所以不加参数时默认会润色；代码层面的缺省值是 `false`，只在配置文件没写该字段时生效。

润色会让每个翻译批次多一次 `strong` 档 LLM 请求，质量可能更稳，但会明显增加耗时和成本。已经翻译完成的批次会被断点续跑跳过，后来再开关润色不会自动重跑旧译文。

## 配置

主要配置都在 `config.yaml`：

- `language.source`: `auto` 由模型识别源语言，也可以写死语言代码，如 `ja`、`en`、`ko`、`ru`、`de` 等。
- `llm.tiers`: 配置 `strong`、`cheap`、`fast` 三档模型。
- `pipeline.review`: 章末审校。
- `pipeline.autofix_severe`: 对严重问题自动重译并采纳通过校验的结果。
- `pipeline.polish`: 翻译后再做中文润色。
- `pipeline.backtranslate_sample`: 回译抽检比例，`0` 为关闭。
- `pipeline.consistency_qa`: 全书跨章一致性扫描。
- `pipeline.book_understanding`: 翻译前预扫整本书，生成全书概览和逐章梗概。
- `pipeline.rolling_context_segments`: 每批翻译时带入的前文译文段数。
- `segment.max_chars_per_batch`: 每个翻译批次的大小。
- `segment.max_chars_per_segment`: 超长段落的拆分阈值。

离线测试或调试流程时，可以把 `llm.provider` 改成 `fake`，不会发网络请求。

## 工作流程

默认连续流程大致是：

```text
读取输入
→ 解析章节、正文段落和 EPUB 目录
→ 模型识别源语言（或使用配置指定语言）
→ 预扫整本书，生成全书概览和逐章梗概
→ 分析样章，建立初始术语表
→ 按章、按批翻译
→ 可选润色
→ 标点规范化
→ 章末 review
→ 可选严重项自动重译
→ 可选一致性 QA
→ 回填导出 EPUB/TXT
```

每个批次翻译完成后都会写入 `state/`，所以长书中断后可以续跑。已经有译文的批次会跳过，只补未完成部分。

## 一致性机制

- **术语库**：人名、地名、专有名词和敬称会进入 SQLite 术语库，翻译时按配置注入提示词。
- **全书理解**：翻译前预扫源文，生成全书概览和章节梗概，让早期章节也能参考全书走向。
- **滚动上下文**：章内批次串行处理，后一个批次能看到前面最近几段译文。
- **段数对齐**：每批输入 N 段，要求模型输出 N 段 JSON；段数不符会重试，仍失败则逐段兜底。
- **章末 review**：按章检查漏译、误译、术语、人称等问题；默认只记录问题，是否自动修复由 `autofix_severe` 控制。
- **标点规范化**：译文统一为简体中文大陆常用全角标点。

## 常用工具

```bash
uv run trans-novel tools glossary book.epub list
uv run trans-novel tools glossary book.epub conflicts
uv run trans-novel tools qa book.epub
uv run trans-novel tools report book.epub
uv run trans-novel tools assemble book.epub
```

这些工具主要用于查看术语库、检查一致性、生成报告或重新导出成品。QA 和报告默认只汇总问题，不会自动改正文。

## 模型档位

默认配置使用 DeepSeek，并通过 OpenAI SDK 调用 `https://api.deepseek.com`。

- `strong`: 翻译、润色、全局分析、标题翻译。
- `cheap`: 章末 review、一致性 QA、回译比对。
- `fast`: 全书预扫、章节梗概、术语抽取、回译等机械任务。

如果模型 ID 变化，直接改 `config.yaml` 里的 `llm.tiers`。

## 项目结构

```text
trans_novel/
  ingest/       输入解析、EPUB/FB2/TXT 切分
  llm/          LLM 抽象接口、DeepSeek provider、FakeClient
  glossary/     SQLite 术语库、抽取、冲突处理
  agents/       分析、翻译、审校、润色、一致性、提示词
  pipeline/     编排器、断点状态、滚动上下文、校验
  postprocess/  标点规范化
  assemble/     EPUB/TXT 回填导出、QA 报告
tests/          离线测试
```

## 测试

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m unittest discover -s tests
```

如果本机 `uv` 缓存目录可写，也可以直接运行：

```bash
uv run python -m unittest discover -s tests
```

## 贡献

欢迎提交 bug 修复、格式兼容、测试样例和文档改进。开发说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

公版书翻译生成的状态目录可在 [wenyi-bookcase](https://github.com/BigDawnGhost/wenyi-bookcase) 查看，也欢迎提交分享；请勿提交或分享无授权的版权文本、私人书籍或包含敏感信息的 `state/` 目录。

项目交流QQ群：1055065098

## 憧憬与不足

本项目为作者个人兴趣所开发，仅在于针对长文本书籍的译介做出一份微薄的努力，未来想让翻译在够准确的前提下更加顺畅，努力从可读向好读迈进。现阶段翻译文本一些口头禅前后翻译不一致，专有名词翻译不准确的问题，已经改进！如果还有什么问题，可以提交issue，如果你有什么想法，欢迎在讨论区提出，如果你有一定的编程能力，欢迎给我提交PR，让这个项目变得更好。👏

## 星标历史

<a href="https://www.star-history.com/?repos=BigDawnGhost%2FWenyi&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&theme=dark&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
 </picture>
</a>
