# 文译

专注于将多语言 EPUB、FB2、TXT 或 Markdown 小说翻译成中文，并尽量保留 EPUB 原排版、图片、目录和跳转。

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

翻译完成后，默认会在源文件目录下生成单语和双语 EPUB，双语版译文在前。运行状态、章节 JSON、术语库和报告会放在 `state/` 目录下。

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

- 输入：EPUB、FB2、TXT、Markdown（`.md` / `.markdown`）。
- 默认输出：中文单语 EPUB 和译文在前的双语 EPUB。
- EPUB 输入会按原 XHTML 模板回填译文，尽量保留原书样式、图片、目录和锚点。
- EPUB 正文按 manifest 声明的媒体类型识别，阅读顺序以 spine 为准。只有媒体类型缺失时才按 HTML 文件后缀兼容识别；声明冲突或无法处理的 spine 资源会明确报错，不会静默跳过章节。
- EPUB 注释采用确定性关系识别：除显式 `epub:type` / ARIA 脚注与返回链接语义外，只接受短符号或 ASCII 数字标记与注释开头返回标记之间可安全解析的双向关系，数字标记还必须有 XHTML 或精确 OPF/NAV 元数据提供的注释语义上下文；类名、文件名、任意文字和单向数字链接都不会被猜成注释。已确认的引用与返回标记不会进入翻译文本，标记链接后的正文仍会保留。
- FB2、TXT 和 Markdown 输入会生成新的 EPUB。
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
```

`--quality` 只覆盖本次运行中需要翻译的章节：`economy` 批量翻译且不润色，`balanced` 单段翻译
且不润色，`quality` 单段翻译并开启检查点批量润色；`--polish` 可单独开启润色。
`--back-matter` 及旧的 `skip/light/full` 处理模式已移除，旧配置中的 `back_matter` 也不再接受。
待译章节始终执行确定性 QA；随后由 `editor` 按单个 lint 问题自动 Repair，每个问题最多 10 次逻辑调用。
候选必须通过完整段落复检才会写回；耗尽预算也会保留安全译文并继续生成单语和双语输出。
已经翻译完成的批次会被断点续跑跳过，完成章节只会复检，不会重新调用正文翻译模型。

新运行使用翻译策略版本 2。旧运行若缺少该版本或版本较旧，不会迁移策略、清空或重译已有结果；
当正文及必需内容节点已经完整时，照常运行同一路径的 `translate` 或 `resume` 会自动进入输出维护，
只补齐排版分析、报告和导出。启用通用 EPUB 主题且排版数据缺失或过期时可能调用 `analyst`；
有效排版数据会直接复用，TXT 输出不会读取主题或调用排版模型。未完成的旧运行仍拒绝补译、润色或
修复，未来策略版本同样拒绝运行。`tools assemble` 仍可用于仅导出。所有路径都保留原策略版本和
已有译文；若要使用新策略，请保留原 `state/`，在另一个工作目录中用源书绝对路径启动新运行。

EPUB 续跑和导出会比较保存的原文槽位与当前解析结果。仅因新规则保护注释标记而产生的兼容差异，
会在核验源文件、语言、策略和完整槽位归属后自动迁移：`auto` 会沿用状态中已保存的语言，显式语言
不一致仍会在写入前拒绝。已有译文不会重新调用模型，事务日志可在中断后继续，并在
运行状态目录内的 `note_migration_backup/` 保留与源文件绑定的迁移前备份。无法证明为注释标记的差异、
新解析导致旧 marker-only 段消失的情况，以及会遗漏直系正文的 mixed-content `aside` 都会在写入前
明确拒绝；其他布局不一致仍须新建运行。

## 章节语义与原文保留

风格分析和术语播种前，`analyst` 通过 `chapter.classify` 阅读每章完整源文，决定整章翻译还是
保留原文。标题、章节位置和 EPUB 结构提示不能单独决定保留；有实质解释、叙事或内容的附录
不能仅因属于书后材料而跳过。

通常每章一次分类请求，不按段落逐一调用。大章按原文顺序分块，每块最多 16000 个 Unicode
字符（不是 token 上限），优先保留段落边界，过长单段连续切开，不以样章抽样代替完整覆盖。
只有所有非空分块都判为 `reference_only` 才整章保留。引用混有实质解释时，即使只有一个分块，
也应分类为 `uncertain`；它表示需要复核，不只表示模型无法判断内容。含 `uncertain` 或分块
分类不一致时，整章仍走正常翻译流程，并标记 `review_required`，不会在同章内只保留部分引用。

每个成功分块立即写入运行目录的 `chapter_classification.json`。同一运行中，原文、标题、
结构提示和策略匹配时，中断后复用已完成分块，只请求剩余分块；完整章节决策另存于章节和
manifest。尚需使用的缓存损坏、源证据变化或决策冲突会报错，不把未完成分类当成保留结果。

保留章不参与风格样章、术语挖掘或术语统一替换，也不调用正文翻译、标题翻译、润色或 Repair。
全书均保留时不再调用风格分析或术语模型。保留章在单语、双语 EPUB/TXT 中都只有一份原文，
保留原标题和目录标签；EPUB 同时保留对应链接、槽位和局部语言语义。共享 XHTML 中只保留
该章范围，不影响相邻待译章；输出仍检查结构与原文保留范围。

`tools report` 和翻译完成摘要会列出保留章、需人工复核章及原因；报告 JSON 的
`chapter_processing.preserved` 和 `chapter_processing.review_required` 保存这两类列表。

## 翻译上下文

全书风格指南由原文样章分析生成，提供有原文依据的表达建议；具体段落原文的事实、语气和文体
优先于全书默认指南。译文保留段落对应和语义，允许在段内按中文习惯调整语序、拆合句和普通标点，
不为了流畅而消除原文刻意的重复或含混，也不额外补充人物心理。

普通正文翻译同时参考原章节标题、同章前两段原文和最近六段译文。每段参考原文最多保留末尾
1200 字符，截断时有标记；不提供后文原文窗口。单段模式每接受一段译文就更新批内历史，
整批提交后才更新共享历史。续跑从已保存的前章及当前章已译前缀重建历史，避免重复或混入后文。

可选润色复用章内检查点批次（默认约 1800 个源文字符），每批发起一次多段请求，并按段落 ID
逐段回填。系统规则、风格指南、章节标题和术语表置于请求前部；批次开始前的原文窗口只传一次，
批次内原文与译文成对提供，不读取相邻批次中异步变化的译文。

响应乱序时按 ID 对应；协议不完整时按既有重试次数重试整批。重试耗尽后只使用最后一次响应：
缺失或空文本项保留该段原译文；重复、未知或非法 ID 等导致整批无法可靠对应时保留整批原译文。
不再追加逐段救援。有效提案仍逐段执行润色门禁，拒绝项保留原译文。

兼容当前翻译策略的旧任务保留已完成润色，待完成批次采用新协议，不自动重润色已有结果；
新批次完成事件记录 `polish_strategy: checkpoint_batch_v1`。不放宽既有策略版本和原文布局检查。
减少重复输入不等于保证缓存命中，也不代表已经证明所有作品的润色质量或速度都会提升。

## 润色拒绝证据

润色门禁仍按“是否新增 lint 问题类型”决定拒绝，不是按问题条数决定。新运行的拒绝记录写入
运行目录 `events.jsonl`，事件名为 `polish_rejected`，包括：

- `source`、`pre_polish_target`、可为空的 `proposal` 全文，以及实际参与检查的
  `checked_raw`、`checked_proposal`。
- `reasons`、`raw_issues`、`proposal_issues`（问题类型与详情），以及请求提交时冻结、
  同时用于 prompt 和门禁的锁定术语映射 `locked_terms`；不重新读取之后变化的术语库。
- 章节和原始段落索引、批次起点与偏移、可为空的资源路径与锚点、内容摘要和稳定的 `audit_id`。
- `strategy: checkpoint_batch_v1` 与 `configured_editor_models`；后者只是配置的候选模型列表，
  不是实际解析到的后端模型身份。

`proposal` 是经过 Agent 清理、JSON 解码后的有效提案，保留去除首尾空白和标点规范化之前的文本，
不是原始 HTTP 响应。无效 JSON、无法可靠对应的 ID、缺失或无效文本，以及没有提案的服务回退，
会记录 `proposal: null` 和 `checked_proposal: null`，不会把原译文冒充模型提案。
协议重试耗尽时只保留最后一次响应中仍可可靠对应的有效提案。

拒绝证据必须先成功追加并同步到磁盘，才提交该批章节和检查点；写入失败会中止，不静默丢弃证据。
中断重试可能出现相同 `audit_id`，日志不承诺恰好一次。该记录也不意味着所有成功请求的完整
prompt 或响应正文都会保存。

这些全文证据可能包含受版权保护的原文、私人文本和术语，请像运行状态一样限制访问，
分享日志前检查并脱敏；本功能不提供额外的自动保留期限或清理机制。

## 配置

Configuration selects models, quality, and output presentation. A config file is optional;
`trans-novel init` writes the example configuration, including these defaults:

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

output:
  mono: true
  bilingual:
    enabled: true
    order: target_first
  override_theme: null
  bilingual_styles: builtin:bilingual
```

- `translator`：正文翻译，默认使用 OpenRouter 上的 Tencent Hy-MT2 30B。
- `analyst`：全章源文语义分类、全局分析、定名和标题翻译。
- `editor`：中文润色与 lint 问题 Repair。
- `fast`：语言识别、术语挖掘和术语抽取。
- 每个角色都必须是非空的 `provider/model-id` 列表；同一角色不能重复候选。
- `quality`：`economy` 使用批量翻译；`balanced` 每次只翻译一个段落并接收纯译文；
  `quality` 在同样的单段翻译调用上增加检查点批量润色。

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

### EPUB themes

To enable source-layout analysis and the packaged Chinese reading style, replace the `output`
mapping in your config with:

```yaml
output:
  mono: true
  bilingual:
    enabled: true
    order: target_first
  override_theme:
    styles: builtin:chinese-reading
  bilingual_styles: builtin:bilingual
```

Use the global `--config` option before the command. For an existing completed run, reassemble
from the same working directory and saved state without calling translation models:

```bash
trans-novel --config config.yaml tools assemble book.epub --format epub
```

**Reassembly can still incur analyst-model cost.** With general styling enabled, missing or
stale source-layout data is analyzed automatically using the `analyst` role. Valid saved results
are reused. Changing CSS alone does not repeat layout analysis. To request a fresh analysis:

```bash
trans-novel --config config.yaml tools assemble book.epub --format epub --reanalyze-layout
```

- The model sees original source markup, context and relevant original CSS, not translated text
  or your output CSS. It returns only semantic roles for host-issued IDs, never executable code.
  Class names are book-local grouping evidence, not universal meanings.
- Groups are sampled and checked against held-out nodes. Conflicting or unknown samples trigger
  per-node analysis of the remaining group, which can approach full-book cost. Rare unsampled
  exceptions can still be missed. Unknown nodes do not default to body. General theme CSS applies
  only to accepted non-null role elements: custom `p` or `*` selectors cannot bypass this boundary.
  Bilingual source styling is independent. Unknown nodes can still inherit CSS from themed
  ancestors; this is not visual isolation.
- Existing CLI output flags override explicit config fields, then saved output choices, then
  defaults. Omitted fields preserve saved choices; `override_theme: null` explicitly clears the
  saved general theme and disables layout analysis. Bilingual-only styling needs no model.
  Disabling both variants normalizes to mono-only.
- `bilingual.order` accepts `target_first` or `source_first`. `bilingual` must be a mapping,
  and a non-null `override_theme` requires `styles`. Remove the old `rules` key: it is rejected,
  not ignored. Unknown fields, unsupported built-ins, empty paths and wrong types are rejected.
- Custom `styles` and `bilingual_styles` paths resolve relative to the config file, with `~`
  expansion. Resolved paths and origins are saved with the source-bound selection.
  A custom bilingual stylesheet replaces the packaged one.
- On first use after the cutover, a valid source-matching version-1 saved presentation selection
  is discarded with `output_selection_obsolete`, not migrated. Supply the new CSS-only config
  above to select general styling; otherwise current defaults apply. Old presentation cache
  does not block automatic missing-profile analysis, and paid translations are untouched.
  Malformed snapshots, unknown versions and source mismatches still fail.
- CSS bytes, effective output choices and the semantic layout profile determine the EPUB output
  digest. CSS/profile changes rebuild output, not paid translations; moving identical CSS does
  not change that digest. Accepted profiles remain reusable after analyst configuration changes.
  Refresh failure stops assembly while retaining the previous accepted profile.
- Source-backed and generated EPUBs use the same role data for mono and bilingual output.
  Bilingual source copies are not reclassified. The original source identity must still match;
  a missing original EPUB cannot be reconstructed from translations to bypass this check.
- Deterministically recognized note markers are excluded from translation while their surrounding
  prose remains in the normal text slots. With `builtin:chinese-reading`, Chinese target forward
  references and backlinks use real `注` link text with a solid brown circular treatment and
  localized EPUB/ARIA semantics. The original IDs, hrefs, anchor structure and note prose remain
  intact. No-theme and custom general styles keep the original marker labels; bilingual source
  copies also keep their original text and attributes. Popup behavior is reader-dependent, so the
  preserved links and backlinks remain the fallback rather than promising the same interaction in
  every reader.
- Navigation, code, covers and preserved source scopes remain protected. Package-declared
  cover/title-page and fixed-layout resources are excluded before layout analysis, avoiding model
  cost and unused-source-CSS failures for those resources.
  CSS supports a restricted reading-style subset; see the [layout and theme contract](docs/epub-theme-design.md).
- TXT output keeps bilingual ordering and saved EPUB selections, but reads no theme assets
  and runs no layout analysis. No bundled fonts or executable theme runtime are required.
- Static source/CSS safety checks are model-free; role-dependent preflight follows analysis
  before body translation. Every requested EPUB is independently verified before any final file
  is replaced. Replacements are sequential, not atomic as a group; a later I/O failure retains
  durable receipts for already published files.

## 工作流程

默认连续流程大致是：

```text
读取输入
→ 解析章节、正文段落和 EPUB 目录
→ 模型识别源语言（或使用配置指定语言）
→ 全章源文语义分类：纯引用章保留原文，其余进入后续翻译流程
→ 分析样章，建立风格指南与初始术语表
→ 启用 EPUB 通用样式时：分析原文排版角色（或复用已保存结果）
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
- **确定性全书 QA**：扫描完成待译章节的源文/译文段落，不调用 LLM，也不修改译文；保留章仍接受输出结构与原文保留检查。
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
- `analyst`：全章源文语义分类、标题翻译、全局分析和定名。
- `editor`：润色。
- `fast`：语言识别、术语挖掘和术语抽取。

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
