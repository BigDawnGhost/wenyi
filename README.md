# 文译

专注于将多语言 EPUB、FB2 或 TXT 小说翻译成中文，并尽量保留 EPUB 原排版、图片、目录和跳转。

项目的日常入口只有一个命令：`translate`。它会完成预扫、分析、翻译、可选润色、确定性 QA、报告和 EPUB 导出；中断后可以继续跑。

## 快速开始

从 [GitHub Releases](https://github.com/turygo/wenyi/releases/latest) 下载与系统匹配的压缩包，并用同一页面的 `SHA256SUMS.txt` 校验文件：

- Windows x64：`wenyi-windows-x64.zip`
- Linux x64 / ARM64：`wenyi-linux-x64.tar.gz` / `wenyi-linux-arm64.tar.gz`
- macOS Intel / Apple Silicon：`wenyi-macos-x64.tar.gz` / `wenyi-macos-arm64.tar.gz`

解压后直接运行：

```bash
export OPENCODE_API_KEY=sk-...
./wenyi translate book.epub
```

在 Windows PowerShell 中，先运行 `$env:OPENCODE_API_KEY = "sk-..."` 设置环境变量，
再运行 `.\wenyi.exe translate .\book.epub`。API Key 来自
[OpenCode Go](https://dev.opencode.ai/docs/go/) 订阅。没有 `config.yaml` 时程序直接使用
OpenCode Go 的 DeepSeek V4 Flash + `balanced` 默认值，不会自动写文件；需要改模型时运行 `wenyi init`。

从源码安装仍可使用 `uv tool install .`，命令名为 `trans-novel`。运行
`trans-novel --help` 或 `wenyi --help` 可查看全部命令。

翻译完成后，默认会在源文件目录下生成译文 EPUB。运行状态、章节 JSON、术语库和报告会放在 `state/` 目录下。

中断后继续：

```bash
trans-novel resume book.epub
```

查看进度：

```bash
trans-novel status book.epub
```

仅重新导出 EPUB：

```bash
trans-novel tools assemble book.epub
```

## 输入和输出

- 输入：EPUB、FB2、TXT。
- 默认输出：中文 EPUB。
- EPUB 输入会按原 XHTML 模板回填译文，尽量保留原书样式、图片、目录和锚点。
- EPUB 正文按 manifest 声明的媒体类型识别，阅读顺序以 spine 为准。只有媒体类型缺失时才按 HTML 文件后缀兼容识别；声明冲突或无法处理的 spine 资源会明确报错，不会静默跳过章节。
- 脚注引用只按 `epub:type="noteref"` 或 `role="doc-noteref"` 识别；无声明的上下标链接按普通文本处理，不根据编号、样式或文件名跳过。
- TXT 输入会生成新的 EPUB。
- 需要纯文本时使用 `--format txt`。

示例：

```bash
trans-novel translate book.epub
trans-novel translate book.epub --format txt
trans-novel translate book.epub --chapter 3
```

## 常用开关

```bash
trans-novel translate book.epub --quality economy
trans-novel translate book.epub --quality quality
trans-novel translate book.epub --polish
trans-novel translate book.epub --source-language ja
trans-novel translate book.epub --back-matter full
```

`--quality` 只覆盖本次运行：`economy` 不润色且附属章走轻量路径，`balanced` 不润色但完整处理
附属章，`quality` 开启润色并完整处理附属章。`--polish` 和 `--back-matter` 可以覆盖当前运行。
确定性 QA 始终执行；随后由 `editor` 按单个 lint 问题自动 Repair，每个问题最多 10 次逻辑调用。
候选必须通过完整段落复检才会写回；耗尽预算也会保留安全译文并继续生成单语和双语输出。
已经翻译完成的批次会被断点续跑跳过，完成章节只会复检，不会重新调用正文翻译模型。

旧运行若缺少当前翻译策略版本，会拒绝续译，不会自动清空或重译已有结果。完整旧运行仍可用
`tools assemble` 导出；若导出需要先补译或修复，则会拒绝。要使用新策略重新翻译，请保留原
`state/`，在另一个工作目录中使用源书绝对路径启动新运行；默认状态写入该工作目录的 `state/`。

EPUB 续跑和导出还会比较保存的原文槽位与当前解析结果。布局不一致时会拒绝复用，并保留
原运行和译文；请新建运行，不要把旧译文强行套入新槽位。布局一致的已保存译文仍可使用。

## 翻译上下文

全书风格指南由原文样章分析生成，提供有原文依据的表达建议；具体段落原文的事实、语气和文体
优先于全书默认指南。译文保留段落对应和语义，允许在段内按中文习惯调整语序、拆合句和普通标点，
不为了流畅而消除原文刻意的重复或含混，也不额外补充人物心理。

普通正文翻译同时参考原章节标题、同章前两段原文和最近六段译文。每段参考原文最多保留末尾
1200 字符，截断时有标记；不提供后文原文窗口。单段模式每接受一段译文就更新批内历史，
整批提交后才更新共享历史。续跑从已保存的前章及当前章已译前缀重建历史，避免重复或混入后文。

可选润色仍逐段返回结果，并使用相同的前文原文窗口；不读取异步变化的邻段译文。
这些改动不新增模型调用轮次，但会增加请求的上下文长度。它们改善信息传递条件，
不代表已经证明所有作品的文学翻译质量都会提升。

## 配置

配置文件只回答“使用什么模型”和“选择哪个质量档位”。没有配置文件也可以直接运行；
`trans-novel init` 会生成以下精简配置：

```yaml
llm:
  models:
    translator:
      - openrouter/tencent/hy-mt2-30b-a3b:off
    analyst:
      - opencode-go/muse-spark-1.3-contributor:low
    editor:
      - opencode-go/muse-spark-1.3-contributor:low
    fast:
      - opencode-go/muse-spark-1.3-contributor:low

quality: balanced
```

- `translator`：正文翻译，默认使用 OpenRouter 上的 Tencent Hy-MT2 30B。
- `analyst`：全局分析、定名和标题翻译。
- `editor`：中文润色与 lint 问题 Repair。
- `fast`：语言识别、术语挖掘、术语抽取和附属章轻量翻译。
- 每个角色都必须是非空的 `provider/model-id` 列表；同一角色不能重复候选。
- `quality`：`economy` 使用批量翻译；`balanced` 每次只翻译一个段落并接收纯译文；
  `quality` 在同样的单段调用上增加逐段润色。

模型规格可在模型 ID 最右侧追加 `:off`、`:low`、`:medium`、`:high` 或 `:max`；
Provider/模型 ID 中间的 `/` 只分割第一个斜杠，因此 OpenRouter 的嵌套模型 ID 可直接使用。
程序启动时会校验整个候选链；不支持的级别直接报错，不会静默升级或降级。
候选会在当前 Provider 的内部重试耗尽后再切换；404 或结构化 `model_not_found` 直接切换，
400/401/403、凭据缺失和本地配置错误立即报错。

Agent 路由、重试、超时、切分、上下文窗口和并发数都是内部策略，不接受 YAML 覆盖。
Provider 使用固定的官方地址和密钥环境变量：

| Provider | 密钥环境变量 |
| --- | --- |
| `opencode-go` | `OPENCODE_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
百炼使用华北 2（北京）的共享 OpenAI 兼容端点。每个角色可以配置不同 Provider，例如：
```yaml
llm:
  models:
    translator:
      - bailian/deepseek-v4-flash:high
      - opencode-go/deepseek-v4-flash:high
    analyst:
      - opencode-go/muse-spark-1.3-contributor:low
    editor:
      - opencode-go/muse-spark-1.3-contributor:low
    fast:
      - bailian/qwen3.7-flash:off
      - opencode-go/deepseek-v4-flash:off

quality: balanced
```

OpenAI 兼容服务仍使用单端点配置；只要候选链引用 `openai-compatible`，就必须配置该端点：

```yaml
llm:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: DASHSCOPE_API_KEY
  models:
    translator:
      - openai-compatible/qwen-max
    analyst:
      - openai-compatible/qwen-max
    editor:
      - openai-compatible/qwen-max
    fast:
      - openai-compatible/qwen-turbo

quality: balanced
```

配置中只写密钥环境变量名，不能写明文密钥。旧的 `llm.provider`、`llm.providers`、
`llm.agents`、标量模型值以及 `pipeline`、`segment` 等格式已废弃，加载时会直接报错；
删除旧文件后直接运行，或执行 `wenyi init --force` 生成新配置。

## 工作流程

默认连续流程大致是：

```text
读取输入
→ 解析章节、正文段落和 EPUB 目录
→ 模型识别源语言（或使用配置指定语言）
→ 分析样章，建立风格指南与初始术语表
→ 源文侧术语候选挖掘 → 一次性全书定名
→ 按章翻译（balanced/quality 每次只提交一个待译段，严格校验单值 JSON）
→ 批后确定性 lint，命中可安全修复的问题即定向重译
→ quality 可选润色（润色若引入 lint 回归，该段回退润色前译文）
→ 标点规范化
→ 确定性全书 QA → 报告
→ 回填导出 EPUB/TXT
```

每个批次翻译完成后都会写入 `state/`，所以长书中断后可以续跑。已经有译文的批次会跳过，只补未完成部分。

## 一致性机制

- **术语库**：翻译前从源文挖掘专名候选，由定名 Agent 一次性统一定名后写入 SQLite 术语库，翻译期只读。
- **滚动上下文**：章内批次串行处理，后一个批次能看到前面最近几段译文。
- **段落对应**：`balanced` / `quality` 每次调用只含一个待译段，并严格要求仅含
  `translation` 键的非空 JSON；格式失败会重试，耗尽后章节失败且不组装成品。
- **经济模式对齐**：`economy` 每批输入 N 段并要求输出 N 段 JSON；段数不符会重试，
  仍失败则逐段兜底。
- **确定性 lint**：零成本机器校验直接引语、数字、锁定专名和未译内容；命中即定向重译或回退，其余记录进报告。
- **确定性全书 QA**：扫描每个完成章节的每个源文/译文段落，不调用 LLM，也不修改译文。
- **标点规范化**：译文统一为简体中文大陆常用全角标点。

## 常用工具

```bash
trans-novel tools glossary book.epub list
trans-novel tools glossary book.epub conflicts
trans-novel tools qa book.epub
trans-novel tools report book.epub
trans-novel tools assemble book.epub
```

这些工具主要用于查看术语库、检查一致性、生成报告或重新导出成品。QA 和报告默认只汇总问题，不会自动改正文。

## 模型路由

每个内部 Agent 固定映射到 `translator`、`analyst`、`editor` 或 `fast` 角色。角色候选按配置顺序尝试：
当前 Provider 的传输层先完成既有重试，只有 fallback 分类器返回固定原因时才切换到下一候选；
404 和结构化 `model_not_found` 直接切换，永久/本地配置错误原样抛出。一次逻辑调用只计一次，
但每个物理尝试仍分别记入用量和遥测。

默认由 OpenRouter 的 Hy-MT2 30B 固定版本翻译正文，由 OpenCode Go 的
Muse Spark 1.2 Contributor 承担分析、润色和快速任务；富文本译文由代码按原文槽位长度确定性回填，保留完整源空白并逐字写入目标值。直接使用 DeepSeek、
OpenAI、百炼或其他 OpenRouter 模型时走各自内置地址；Ollama 与 vLLM 使用本地默认地址；
`openai-compatible` 必须设置 `llm.base_url`。

内部 Agent 固定映射到四个用户模型角色：

- `translator`：正文翻译和定向重译。
- `analyst`：标题翻译、全局分析和定名。
- `editor`：润色。
- `fast`：语言识别、术语挖掘、术语抽取和附属章轻量翻译。

thinking 级别由模型规格最右侧的后缀决定。程序按逐 Provider、逐模型能力表生成请求字段；
配置模型不支持的 thinking 级别会在启动时直接报错。

## 项目结构

```text
trans_novel/
  config.example.yaml  默认配置的唯一来源，首次运行时复制到工作目录
  ingest/       输入解析、EPUB/FB2/TXT 切分
  epub/         共享包模型、目录与文本槽位；统一 manifest/spine 解析和资源分类
  llm/          LLM 抽象接口、provider factory、内置 providers、FakeClient
  glossary/     SQLite 术语库、源文候选挖掘、译后抽取（可选）、冲突处理
  agents/       分析、翻译、润色、定名、提示词
  pipeline/     application、composition、execution、nodes、planning、quality、state 能力模块
  postprocess/  标点规范化
  assemble/     EPUB/TXT 回填导出、QA 报告；epub/publication、verification、rendering 分层
  benchmark/    corpus、run、integration、review、report 离线评估能力模块
tests/          按能力模块组织的离线测试；共享测试夹具位于 tests/fixtures/
```

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m unittest discover -s tests
```

如果本机 `uv` 缓存目录可写，也可以直接运行：

```bash
uv run python -m unittest discover -s tests
```
## 本地模型基准

本地模型基准使用独立的 `benchmarks/` 工作区，不属于主应用配置。完整布局、配置格式和操作命令见
[`docs/benchmark-guide.md`](docs/benchmark-guide.md)。

## 提交与发版

启用提交钩子：

```bash
uv run pre-commit install
```

提交涉及代码、测试、构建脚本、GitHub Actions 工作流或依赖的变更时，必须在同一次提交中更新
`CHANGELOG.md` 的 `[Unreleased]` 小节。否则，提交钩子会拒绝该提交，Pull Request 的 CI
也会执行相同检查。

发布 `X.Y.Z` 版本时：

1. 把 `pyproject.toml` 中的 `project.version` 修改为 `X.Y.Z`。
2. 在 `CHANGELOG.md` 中保留新的 `[Unreleased]` 小节，并把本次发布内容移入
   `## [X.Y.Z] - YYYY-MM-DD` 小节。
3. 提交对版本号和 `CHANGELOG.md` 的修改。
4. 创建并推送与版本号完全匹配的标签：`git tag vX.Y.Z && git push origin vX.Y.Z`。

标签推送会触发 GitHub Actions。只有当标签、`project.version` 与 `CHANGELOG.md` 中的版本号
完全一致时，工作流才会创建 GitHub Release，并以对应版本小节作为发布说明；随后附上各平台的
可执行文件和 `SHA256SUMS.txt`。

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
