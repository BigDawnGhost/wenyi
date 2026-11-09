# CLI 使用指南

CLI 用于翻译本地文件、续跑、检查术语、执行全书 Review 和导出译本。
本指南使用安装后的 `wenyi` 命令。图形界面操作分别见
[Desktop](desktop.md)、[Web](web.md) 和[工作区指南](workspace.md)。

- [配置参考](../reference/configuration.md)：设置、默认值、提供商与模型路由。
- [翻译流程](pipeline.md)：译前准备、翻译、润色和 Review 的作用。
- [格式参考](../reference/formats.md)：输入支持、输出命名、排版和格式限制。
- [开发指南](../development/index.md)：从源码运行与参与开发。

<a id="installation"></a>

## 安装与配置

### 安装 CLI

使用 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/) 安装已发布的
[wenyi-cli](https://pypi.org/project/wenyi-cli/)，无需获取源码：

```bash
uv tool install wenyi-cli
wenyi --version
```

如果找不到 `wenyi`，执行 `uv tool update-shell` 后重新打开终端。
后续使用 `uv tool upgrade wenyi-cli` 升级。也可以在已激活的虚拟环境中执行
`uv pip install wenyi-cli`；该环境还支持 `python -m wenyi_cli`。

独立 CLI 压缩包不需要 Python。选择匹配平台的发行文件，先用
`wenyi-cli-SHA256SUMS.txt` 核对，再解压。以下文件名中的 `1.2.3`
应替换为下载版本：

| 平台 | 压缩包 |
|---|---|
| Windows x64 | `wenyi-cli-1.2.3-windows-x64.zip` |
| Linux x64 / ARM64 | `wenyi-cli-1.2.3-linux-x64.zip` / `wenyi-cli-1.2.3-linux-arm64.zip` |
| macOS Apple Silicon / Intel | `wenyi-cli-1.2.3-macos-arm64.zip` / `wenyi-cli-1.2.3-macos-x64.zip` |

Windows 解压后使用 `.\wenyi.exe`。Linux/macOS 先执行一次 `chmod +x wenyi`，
再使用 `./wenyi`。macOS 版本使用 ad-hoc 签名，没有 Apple notarization；
确认校验和无误后，如被系统拦截，可在**系统设置 → 隐私与安全性**中批准运行。
`wenyi-desktop-*` 是独立的 [Desktop 安装包](desktop.md)。

### 创建并检查配置

在准备工作的目录运行：

```bash
wenyi models list
wenyi languages
```

缺少配置时，CLI 会创建带注释的 `config.yaml`，帮助和版本命令也会执行此检查。
`models list` 只预览实际模型路由，不发送请求。
翻译前检查提供商、语言、流程与输出设置。

使用根选项 `--config`（`-c`）指定其他文件，选项放在命令前：

```bash
wenyi --config my-config.yaml models list
wenyi -c my-config.yaml models check --for translate
```

`models check` 只在本地检查可达操作的凭据，不能证明远端账号或模型可用。
可选的真实连通性测试见[模型命令](#model-commands)。

CLI 的 API Key 只从进程环境变量读取，不会自动加载仓库根目录 `.env`。
请在本机设置，或从共享工作区／会话之外的私有密钥文件加载到环境中。
不要将真实密钥写入 YAML、版本库、共享日志或对话。下方示例只使用占位符。

<a id="first-translation"></a>

## 首次翻译

默认配置使用 DeepSeek，目标语言为简体中文。Linux/macOS Shell：

```bash
export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
wenyi translate book.epub
```

Windows PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = "YOUR_DEEPSEEK_API_KEY"
wenyi translate .\book.epub
```

使用解压版时，将命令替换为 `./wenyi` 或 `.\wenyi.exe`。
仅提交有权发送给所选提供商的文档。准备、翻译、润色和 Review 都可能产生付费模型请求；
PDF 导入还可能把文档发送给所选解析服务。

默认 EPUB 翻译会将译本写入源文件旁的 `output/`，通常为 `book.zh.epub`。
终端会显示实际路径、章节与术语数量、用量、耗时和 Review 结果。

**默认开启润色、全书 Review 和 Review Autofix。** 翻译结束后，
Review 可能发布修订，修改已保存的正式译文。
若要保留 Review 报告但不启动新的修订发布，请在 `translate` 前将 YAML 中的
`pipeline.review_autofix` 设为 `false`。若本次完全跳过最终 Review，使用
`--no-review`。`--no-autofix` 属于独立 `review` 命令，不是 `translate` 参数。
已经开始的 Autofix 发布仍可能在续跑时恢复，详见 [Review 安全边界](#review)。

### 选择语言

将以下片段合并到配置中，保留原有模型设置，即可直接中译英：

```yaml
language:
  source: zh
  target: en
```

```bash
wenyi --config config.yaml translate book.epub --bilingual
```

`source: auto` 会调用模型识别源语言；显式指定可跳过这次请求。
每次运行选择一个方向。后续状态、术语、Review 和导出命令应使用同一目标语言配置。
[支持的语言代码](../reference/configuration.md#languages) 是实验性翻译支持，
不代表母语质量保证。CLI 帮助与诊断仍使用英语。

### 先解析或准备

```bash
wenyi parse book.epub
wenyi prepare book.epub
wenyi translate book.epub
```

- `parse` 列出**从零开始**的章节编号、标题和段落数，并在显示的状态目录缓存解析原文。
  不需要 LLM 凭据，不调用模型识别语言，也不初始化翻译状态。
  PDF 解析仍需要配置的 MinerU/BabelDOC 服务。
- `prepare` 解析原文、按需识别语言、分析风格与人物语气，并在启用时生成章节梗概和全书概览。
  **它既不翻译正文，也不抽取或预填术语。** 此命令可能产生模型用量。
- `translate` 复用匹配的解析与准备结果，再处理待译内容。
  常规术语抽取从翻译过程中完成的原译文对开始。

首次通过 MinerU 导入未缓存的 PDF，还需在进程环境中设置 `MINERU_API_KEY`。
BabelDOC 配置、PDF 导出条件及其他格式要求见[格式参考](../reference/formats.md)。

<a id="translation-options"></a>

### 翻译参数

未传入的命令行选项沿用 YAML 设置。

| 选项 | 作用 |
|---|---|
| `--chapter N` | 翻译并保存一个从零开始编号的章节，跳过全书 Review 和导出。 |
| `--polish` / `--no-polish` | 覆盖待译任务的 `pipeline.polish`。 |
| `--translation-mode standard\|three_draft_synthesis` | 覆盖翻译方式；精翻要求开启润色。 |
| `--review` / `--no-review` | 覆盖自动最终全书 Review。 |
| `--format epub\|txt\|html\|markdown\|pdf\|docx` | 选择导出格式；PDF 仅适用于已保存的 BabelDOC 项目。 |
| `--out PATH` | 指定单语输出路径；双语命名从此派生。 |
| `--mono` / `--no-mono` | 覆盖单语输出开关。 |
| `--bilingual` / `--no-bilingual` | 覆盖双语输出开关。 |
| `--progress-json PATH` / `--result-json PATH` | 写入可选的[机器进度与回执](#machine-output)。 |

```bash
wenyi translate book.epub --chapter 3
wenyi translate book.epub --no-polish --no-review
wenyi translate book.epub --bilingual
wenyi translate book.epub --no-mono --bilingual
```

`--chapter 3` 指第四章。单章翻译不应与收尾选项混用：
`--out`、Review 开关、单／双语开关和非 EPUB 的 `--format` 值都会被拒绝。
之后用 `assemble` 导出。默认格式、显式输出文件名和双语限制统一见
[格式参考](../reference/formats.md)。

<a id="precision-translation"></a>

### 三译合润精翻

```bash
wenyi translate book.epub --translation-mode three_draft_synthesis --polish
```

该模式生成**三份独立初稿，再做一次对照原文的综合润色**，
不是从三份中选一份。该模式要求润色，不能与 `--no-polish` 合用。
正常批次调用模型四次，标准翻译加润色为两次；不保证质量提升。

续跑时保持相同模式与配置，以复用精翻检查点。改为 `standard` 只影响待译批次，
不重做已完成译文。质量与成本取舍见[三译合润流程](pipeline.md#three-draft-precision)。

<a id="prompt-genres"></a>

### 翻译风格

根选项 `--prompt-genre literary|academic` 选择本次 CLI 调用的翻译风格，放在子命令前。
默认 `literary`；`academic` 侧重论证、概念区分、限定条件和引文。
CLI、Web 与 Desktop 共用这两套提示词；这不是自动题材识别，也不是 Core YAML 设置。

```bash
wenyi --prompt-genre academic --config academic.yaml prepare book.docx
wenyi --prompt-genre academic --config academic.yaml translate book.docx
wenyi --prompt-genre academic --config academic.yaml review book.docx --no-autofix
```

旧的 `--experimental-prompt-genre` 写法仍兼容已有脚本，但不再显示在帮助中。
新命令使用 `--prompt-genre`；同时传入两种写法时，值必须一致。

每次执行 `prepare`、`translate` 和 `review`（包括续跑）都应传入相同选项，
不会从保存状态恢复。切换可能刷新派生分析和待完成工作，但不重译已完成文本。
对照试验需使用独立的 `paths.state_dir`，仅换输出文件名不能隔离状态。
两种方案都会产生用量，仍需代表性文本质量评估。
Web/Desktop 的创建时选择见[工作区指南](workspace.md)。

<a id="terminology-context"></a>

### 实验性术语证据

设置 `pipeline.terminology_context: true`，可开启仅 CLI 支持的全书人物原文证据。
使用前先检查路由：

```bash
wenyi models explain --operation terminology.evidence
wenyi models check --for translate
wenyi translate book.epub
```

此功能在待译批次中增加人物发现和取证；`prepare` 仍不抽取或建立术语。
其他类型保持常规译后抽取。已有权威译名和受保护的人工字段保留。
它会增加上下文、模型调用和首批延迟，默认关闭，也不是逐次出现的词义选择系统。
Review 可以消费已有备注，但不生产这类证据。
用于长书前请阅读[上下文与术语](pipeline.md#context-and-glossary)。

<a id="resume"></a>

## 续跑与检查状态

完成的批次保存在 `<paths.state_dir>/<书名>/targets/<目标语言>/`。
中断后，使用相同源文件和配置重复原命令：

```bash
wenyi translate book.epub
wenyi status book.epub
wenyi inspect-run book.epub
```

完成译文会跳过。更换模型或润色设置不会自动重做这些译文。
升级或改变提示词／语言策略可能在处理待完成工作前重建部分准备结果，
这会产生模型用量，但不替换已完成译文。

状态绑定源内容，不只是文件名。同名但内容不同、源语言与状态冲突或保存的原文哈希无效，
均不能续跑。需要全新翻译时，选择新的 `paths.state_dir`；
不要只为试另一项设置而删除旧状态。CLI、Web 和 Desktop 不共享项目状态。

Ctrl+C 会协作式停止排队请求与重试等待；正在执行的请求可能完成或等待到提供商超时。
强杀可能丢失最后未保存的工作或用量，优先正常中断。

`status` 显示章节进度、术语统计与计时。`inspect-run` 不调用模型，
只汇总已记录的失败、重试、耗时及所选目标累计用量。加 `--verbose` 可显示最后记录的
traceback；分享前检查其中的私有文本和路径。这两个查看命令不增加执行时长。

Token 总量跨续跑累计，不仅属于上次调用。缓存读取率为已报告读取占全部输入的比例；
缺少计数表示“未知”，不是零。这些计数不是金额估算。
工作流计时不包含两次命令之间的停机时间；强杀或初始化前失败可能无法保存本次时长。

### 诊断日志

CLI 不再把常规应用日志和 HTTP 客户端日志混入终端进度条。
每次调用将 INFO 及以上级别的诊断信息保存到所选配置文件旁独立的
`logs/wenyi-cli-<UTC 时间戳>-<进程号>-<唯一后缀>.log`。
这个位置不受 `paths.state_dir` 影响；使用 `--config /work/settings.yaml` 时，
日志位于 `/work/logs/`。

进度、结果和需要处理的错误提示仍会显示。命令失败时，CLI 会打印诊断日志的绝对路径。
日志无法初始化时，命令会简洁报错并停止，不会静默丢弃诊断信息。
`WENYI_LOG_FORMAT=json` 只切换日志文件中的记录格式，不额外输出到终端。
Web 和 Desktop 的日志策略不变。

单文件按 5 MiB 轮转，最多保留 `.1`、`.2` 两份备份。
不同调用不共用日志文件，也不会删除旧调用的日志。
分享前检查其中的私有文本和路径，不再需要的日志应显式清理。

<a id="proofreading"></a>

## 术语检查与校对

```bash
wenyi glossary list book.epub
wenyi glossary conflicts book.epub
wenyi glossary resolve book.epub "source term" "chosen translation"
```

`resolve` 修改**已有**词条的权威译名并关闭冲突，不是术语导入或全书查找替换命令。
后续翻译和 Review 可以使用该选择；已完成段落仍需结合导出文本与 Review 结果检查。
没有独立的 `polish` 命令：润色发生在翻译过程中，后来打开开关不会润色已完成批次。

<a id="review"></a>

## 全书 Review

所有章节必须已经完成正文翻译。只生成报告、不启动新 Autofix 发布：

```bash
wenyi review book.epub --no-autofix
```

**`wenyi review book.epub` 默认并非只读。** 它继承
`pipeline.review_autofix`，内置默认值为 `true`，因此可能发布修订到正式章节译文。
`--autofix` 和 `--no-autofix` 是可选覆盖，不是另一套默认行为：

```bash
wenyi review book.epub --autofix
```

Review 结合术语检查全书，可生成临时修订并盲审复查。
使用 `--no-autofix` 会保存报告与本次运行的临时修订，不启动新的正式写回。
**如果此前 Autofix 已保存 `applying` 发布记录后中断，
Review 会先恢复该写回，即使本次使用了 `--no-autofix`。**
该参数不是对已开始发布的取消或回滚。
仅关闭 `review_fix_loop` 也**不会**关闭 Autofix 发布。
Review Autofix 不会重写术语库。

Review **不保证每次都新建运行**。内容、有效 Review 配置／模型、指导与术语身份匹配时，
可以复用已完成结果或续跑未完成任务。相关身份改变才新建；
Autofix 修改正式文本后，也可能改变下次 Review 的输入。

终端会显示 `state/<书名>/targets/<目标语言>/reviews/review-<时间戳>/` 下的结果目录。
`result.json` 是统一结果；使用 Autofix 时，`autofix/index.json` 记录发布判定和前后变更。
模型检查不能代替人工校对。发布修订后，重新执行 `assemble` 导出。
概念与安全边界见 [Review 与 Autofix](pipeline.md#review-and-autofix)；
内部检查点和协议见[流程开发文档](../development/pipeline.md)。

<a id="export"></a>

## 导出已有译文

```bash
wenyi assemble book.epub
wenyi assemble book.epub --format txt --out output/reading-copy.txt
wenyi assemble book.epub --bilingual
wenyi assemble book.epub --no-mono --bilingual
```

`assemble` 从完整或部分完成的保存状态导出，不调用模型。
接受 `--format`、`--out`、`--mono/--no-mono` 和
`--bilingual/--no-bilingual`，含义与 `translate` 相同。请保留匹配的原始文件。

另一个进程仍在翻译时，导出读取已保存工作的一致快照，之后完成的批次需要再次导出。
仅影响输出的设置通常只需重新导出，无需重译。
BabelDOC PDF 导出仍需请求外部 bridge。
默认格式、双语命名、样式保留、PDF 要求和不支持的组合见[格式参考](../reference/formats.md)。

<a id="automation"></a>

## 自动化与命令速查

<a id="command-reference"></a>

### 命令表

所有书籍命令接收原始源文件路径，不是导出的译本。
根选项放在命令前，命令选项放在命令后。
通过 `wenyi --help`、`wenyi COMMAND --help` 或
`wenyi models COMMAND --help` 查看已安装版本的语法。

| 命令 | 参数／选项 | 用途 |
|---|---|---|
| `parse` | `INPUT` | 解析并列出原文章节，不调用模型。 |
| `prepare` | `INPUT` | 仅准备，可能调用模型。 |
| `translate` | `INPUT`、[翻译参数](#translation-options) | 全流程或单章翻译，重复执行续跑。 |
| `review` | `INPUT`、`--autofix/--no-autofix` | 全书 Review，可覆盖发布设置。 |
| `assemble` | `INPUT`、[导出参数](#export) | 导出保存的译文，不调用模型。 |
| `status` | `INPUT` | 章节进度与术语统计。 |
| `inspect-run` | `INPUT`、`--verbose` | 已保存的失败、重试、耗时和用量。 |
| `glossary list` | `INPUT` | 已有术语与状态。 |
| `glossary conflicts` | `INPUT` | 未解决术语冲突。 |
| `glossary resolve` | `INPUT SOURCE TARGET` | 裁定已有词条。 |
| `languages` | 无选项 | 无需凭据，列出内置语言代码。 |
| `models` | [下方子命令](#model-commands) | 路由、检查、真实探测和用量账本转换。 |
| `language-policy` | 开发诊断 | 见[语言策略诊断](../development/pipeline.md#language-policies)，不是翻译设置。 |

根选项为 `--config/-c PATH`（默认 `config.yaml`）、
`--prompt-genre literary|academic`（默认 `literary`）、
`--version` 和 `--help`。
已移除的 `report` 命令不可用，请使用 `status`、`inspect-run`、术语命令和 Review 结果。

<a id="model-commands"></a>

### 模型命令

| 命令 | 选项 | 网络行为 |
|---|---|---|
| `models list` | `--json` | 查看实际路由，无需凭据、不发送请求。 |
| `models explain` | 必填 `--operation ID` | 输出一个路由的 JSON 说明，无需凭据、不发送请求。 |
| `models check` | `--for prepare\|translate\|review`（默认 `translate`） | 本地检查可达凭据，不发送请求。 |
| `models ping` | `--for prepare\|translate\|review`、`--timeout SECONDS`（默认 `30`，最小 `0.1`）、`--json` | 对每个可达模型配置发送真实请求，包括备用模型。 |
| `models migrate-usage` | `RUN_DIR` | 离线转换所选 CLI 用量账本。 |

`list --json`、`explain` 和 `ping --json` 向标准输出打印结构化 JSON。
其他表格／进度面向人阅读，不是稳定解析接口。
`check` 和 `ping` 根据 YAML 的流程开关检查；`translate` 则先应用自己的阶段覆盖再校验凭据。

`ping` 需显式调用，会消耗 token，但不计入书籍账本。它不重试，使用短请求，
通常输出预算为 16 token 或提供商最低要求（如 thinking 模式的 4096）。
空或截断回复也算可连通，但不代表任务质量。任一配置失败则退出码为 1，
失败按类别报告，不回显原始提供商响应。

<a id="usage-ledgers"></a>

### 用量账本

```bash
wenyi models migrate-usage state/BOOK/targets/zh
```

用量账本属于运行数据，不随设置重建。非空的路由升级前账本需要此显式转换，
未知提供商／模型历史标为 `unknown`。Schema-2 缓存用量可读取，
在下次正常保存时持久化为当前格式，不会猜测缺失的历史缓存分类。

转换用量前先停止所选目标的工作流。命令处理该 CLI 目标及其 Review 账本，保留总量，
并在变更文件旁保存原始字节备份 `usage.before-routing-<timestamp>.json`。
先校验后写入，逐文件原子发布。中断后重复同一命令，已转换账本会跳过。
它不迁移源书或 Web/Desktop 工作区。

<a id="machine-output"></a>

### 机器进度与输出回执

```bash
wenyi translate book.epub \
  --progress-json /absolute/progress.json \
  --result-json /absolute/result.json
```

两项均可选，仅用于 `translate`。写入 protocol version 1 JSON，
通过目标目录中的原子替换发布，终端输出不变。
请使用专用路径，彼此及与源文件、配置、所有输出文件均不相同；
CLI 会拒绝与源文件、配置及显式 `--out` 路径冲突。

每次调用生成新的 `run_id`。文件包含 `protocol_version`、`run_id` 和 `updated_at`。

| 文件／状态 | 字段与含义 |
|---|---|
| 运行中进度 | `status: "running"`、`done`、可为空的 `total`、`label`；计数只描述当前阶段，不是全流程百分比。 |
| 预检前结果 | `status: "running"`、空 `outputs`，替换旧回执。 |
| 成功结果 | `status: "success"`、`source_sha256`、绝对 `state_dir`；`outputs` 含绝对 `path`、字节 `size` 和 `sha256`。 |
| 失败结果 | `status: "failed"`、空 `outputs` 和通用 `error`；详情在终端／日志中。 |

单章翻译不导出，成功回执的输出清单为空。
在回执初始化前失败或无法写入回执时，可能没有本次新回执。
强杀的进程可能留下 `running`。

管理程序发布附件前必须检查**进程退出状态**、匹配当前 `run_id` 并校验文件哈希，
不能仅凭进度文件判断存活。这些文件不是任务队列、常驻服务或取消接口。
路径、报告与回执可能透露私有文档信息，分享前请检查。

<a id="exit-status"></a>

### 退出状态

- `0`：命令执行成功，不表示翻译质量已认证或 Review 没有发现问题。
- `1`：可预期的运行／配置错误、缺少状态或凭据、请求预算停止、模型连通性探测失败等。
- `2`：命令行用法／参数错误，包括不支持的输出格式值和超出范围的章节选择。

自动化应将所有非零退出或强制终止视为失败，不要假定每个错误都属于某个固定类别。
结合诊断与回执排查，解决原因后用原源文件和配置续跑。
