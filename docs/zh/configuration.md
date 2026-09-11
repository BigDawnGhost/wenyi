# 配置说明

[English](../configuration.md)

程序读取当前工作目录的 `config.yaml`。配置文件不存在时会自动创建带注释的默认文件。

顶层配置项为 `language`、`llm`、`segment`、`pipeline`、`output`、`honorific` 和 `paths`。未知配置项会被拒绝；已移除的设置不会自动转换为新格式。

## 语言

```yaml
language:
  source: auto
  target: zh
```

`source: auto` 会调用模型识别源语言；也可以显式选择下表语言。源语言与目标语言可直接互译，不经中文中转。多语言质量仍属实验性。默认 CLI、配置注释和提示词指令统一使用英语，与翻译目标独立。生成的默认配置仍为 `target: zh`；需要英语译文时选择 `en`。

所有模型生成的说明性元数据（包括术语 `note`、风格指南、人物描述及说明中的人物称呼）均明确要求使用目标语言。人物 `target` 保存翻译或音译后的姓名；`source` 和 `aliases` 保留原文拼写以供匹配，证据引用也可以包含原文。类型和性别使用英语标识符，不再转换旧中文枚举。续跑会保留原有分析和备注，提示词更新不会自动翻译旧元数据。若要重新分析并进行全书比较，请使用独立的 `paths.state_dir`。

| 代码 | 语言 |
|---|---|
| `zh`、`zh-Hant` | 简体中文、繁体中文 |
| `en`、`en-US`、`en-GB` | 英语、美式英语、英式英语 |
| `ja`、`ko` | 日语、韩语 |
| `fr`、`de`、`es`、`it` | 法语、德语、西班牙语、意大利语 |
| `pt`、`pt-BR`、`pt-PT`、`ru` | 葡萄牙语、巴西/欧洲葡萄牙语、俄语 |

运行 `uv run trans-novel languages` 查看内置列表，无需 API Key。`target` 不接受 `auto`；不支持的代码在配置校验时拒绝。注册的语言别名 `zh-Hans` / `zh-CN` → `zh`、`zh-TW` → `zh-Hant`、`ja-JP` → `ja`、`ko-KR` → `ko`；已注册的地区和文字变体保留，不再截取前两个字母。

每次运行选择一个方向。例如 `source: zh`、`target: en` 直接中译英；把日语原文设为 `source: ja`、`target: en` 则直接日译英。检测或规范化后完全相同的语言会拒绝翻译。更换目标语言会建立独立状态；`prepare`、`translate`、`review`、`assemble`、`status`、`report` 和术语命令须使用对应的 `language.target`。源语言显式配置与保存值冲突时拒绝续跑。

提示词目录、状态布局和首版验证范围见 [P10 国际化实现与后续方案](project-review/2026-09-05/p10-multilingual-internationalization.md)。

## 模型与操作路由

保留三个便捷档位，也可以独立覆盖某个操作，或混用多个提供商连接。最简配置：

```yaml
llm:
  preset: deepseek
```

该预设展开为连接 `default`、模型配置 `default_strong` / `default_cheap` / `default_fast`，以及三个档位映射。内置产品默认值为 `https://api.deepseek.com`、环境变量 `DEEPSEEK_API_KEY`；三个档位均使用 `deepseek-flash`，开启 thinking，`reasoning_effort` 为 `high`。模型 ID 与默认推理设置依据 [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/api/create-chat-completion/)。档位保持独立映射，便于之后分别覆盖模型；预设不会自动查询远端能力。也支持 `preset: gemini` 和离线的 `preset: fake`。

例如，单独配置润色与取证模型：

```yaml
llm:
  preset: deepseek
  providers:
    editorial:
      kind: gemini
      api_key_env: GEMINI_API_KEY
      timeout: 120
      max_retries: 2
      max_concurrency: 2
  models:
    editor:
      provider: editorial
      model: YOUR_EDITOR_MODEL
      max_output_tokens: 8192
      options:
        thinking_level: high
  routes:
    polish.body: {model: editor}
    review.verify: {model: editor}
```

将 `YOUR_EDITOR_MODEL` 换成端点支持的模型。其他操作继续使用默认档位；没有单独覆盖时，`autofix.verify` 继承已解析的 `review.verify` 路由。

### 配置规则

- `providers.<id>` 定义连接：`kind`、可选的 `base_url`、`api_key_env`、`timeout`（秒，默认 600）、`max_retries`（额外尝试次数，默认 4）、`max_concurrency`（不设置则不限制）和可选 `quota_group`。
- `models.<id>` 定义请求配置：`provider` 连接 ID、远端 `model` ID、可选的正数 `max_output_tokens` 和提供商专属 `options`。
- `tiers` 必须恰好将 `strong`、`cheap`、`fast` 映射到模型配置；不用预设时需完整填写，也可以全部指向同一模型。档位表示偏好，不代表实测质量或价格。
- `routes.<操作>` 必须且只能选择 `{model: 配置名}` 或 `{tier: strong}`。未知操作、字段及引用在请求前报错，不再隐式回退缺失档位。
- 覆盖预设时，按 ID 整体替换连接或模型配置，需要重复必填字段；不同模型的 options 不合并。档位和路由按各自键替换。
- 显式 `max_output_tokens` 优先于流程的静态、动态输出提示；不设置时，梗概和注释定位沿用原有提示预算。OpenAI 兼容适配器在 thinking 开启时将低于 4,096 的提示预算提升到 4,096；显式设置更小上限则报错。实际模型限制仍取决于服务端。
- API Key 只从环境变量读取，不写入地址或原始请求扩展。原始扩展不能覆盖模型身份、消息、流式开关、JSON 模式、密钥和输出上限。

### 提供商选项

| 适配器 | 连接默认值 / 选项 | 模型选项 |
|---|---|---|
| `deepseek` | DeepSeek 端点；`DEEPSEEK_API_KEY` | `thinking`、`reasoning_effort`、`extra_body` |
| `openai` | OpenAI 端点；`OPENAI_API_KEY` | `thinking`、`reasoning_effort`、`extra_body` |
| `openrouter` | OpenRouter 端点；`OPENROUTER_API_KEY` | `thinking`、`reasoning_effort`、`extra_body` |
| `gemini` | 原生 Gemini API；未指定自定义变量时，从 `GEMINI_API_KEY` 回退到 `GOOGLE_API_KEY` | `thinking_level` 或 `thinking_budget`、`temperature`、`extra_body` |
| `openai-compatible` | 必填 `base_url`；可选 `api_key_env`；`reasoning_style` | `thinking`、`reasoning_effort`、`json_response_fallback`、`request_overrides` |
| `orcarouter` | `https://api.orcarouter.ai/v1`；`ORCAROUTER_API_KEY`；`reasoning_style` | 同 `openai-compatible` |
| `ollama`、`vllm` | `http://localhost:11434/v1`、`http://localhost:8000/v1`；可选密钥；`reasoning_style` | 同 `openai-compatible` |
| `fake` | 无网络、无需密钥 | 无提供商选项 |

兼容端点的 `reasoning_style` 支持 `none`（默认）、`deepseek`、`openai`、`openrouter`。只有明确配置 `json_response_fallback: reasoning_content`，才会从网关的该字段读取有效 JSON；默认 `none`，非 JSON 推理文本不会被当作结果。Gemini 的 thinking level 和 budget 互斥。原始扩展字典依赖具体端点；离线校验无法保证远端模型接受这些参数。

SDK 内置重试统一关闭。Wenyi 统一重试连接/超时、HTTP 408/409/429、5xx 瞬时错误及空响应；退避期间释放连接并发名额，并响应取消。普通 4xx 错误不重试。PDF 默认 MinerU 解析另用 `MINERU_API_KEY`；可选 BabelDOC HTTP bridge 独立于模型路由。

DeepSeek 的 `reasoning_effort` 可设为 `low`、`high` 或 `max`；`thinking: false` 显式关闭思考，此时不发送推理强度。未配置输出上限且流程没有输出提示时，由服务采用默认上限：非思考模式 8K、思考模式 64K，`max` 强度下为 128K。流程提示和显式 `max_output_tokens` 仍按上述配置规则处理。详见 [DeepSeek 请求参数](https://api-docs.deepseek.com/api/create-chat-completion/)。

### 已注册操作

| 操作 | 默认档位或继承 | 用途 |
|---|---|---|
| `language.detect` | `cheap` | 源语言识别 |
| `analysis.style` | `strong` | 风格、人物与初始术语分析 |
| `synopsis.chapter` | `fast` | 章节梗概；600 token 输出提示 |
| `synopsis.book` | `fast` | 全书概要；1,200 token 输出提示 |
| `translation.body` | `strong` | 正文翻译及段落对齐恢复 |
| `translation.title` | `strong` | 章节与目录标题 |
| `polish.body` | `strong` | 译文润色 |
| `glossary.extract` | `fast` | 术语抽取 |
| `glossary.align_history` | `fast` | 历史译法对齐 |
| `annotation.align` | `cheap` | 注释定位；动态输出提示 |
| `review.scan` | `cheap` | 初审与盲审复查 |
| `review.verify` | `strong` | 取证核查 |
| `review.arbitrate` | `strong` | 冲突仲裁 |
| `review.fix` | `strong` | 影子修订 |
| `autofix.verify` | `review.verify` | 发布前取证核查 |
| `autofix.fix` | `review.fix` | 正式发布修订 |
| `srt.translate` | `strong` | 字幕批次与单条恢复 |

### 预览、限额与显式故障切换

```bash
uv run trans-novel models list
uv run trans-novel models list --json
uv run trans-novel models explain --operation review.verify
uv run trans-novel models check --for translate
```

`list` 和 `explain` 无需密钥；`check --for prepare|translate|review|srt` 只检查当前配置开关下可达操作的密钥。这三个命令均不创建 SDK 客户端、不发送请求。翻译命令先应用 CLI 流程开关，再检查密钥。

可选本地控制示例（使用离线提供商）：

```yaml
llm:
  preset: fake
  providers:
    default:
      kind: fake
      max_concurrency: 2
      quota_group: account
  models:
    bounded:
      provider: default
      model: fake
      max_output_tokens: 2048
  tiers: {strong: bounded, cheap: bounded, fast: bounded}
  quotas:
    account:
      requests_per_minute: 20
      tokens_per_minute: 60000
  budget:
    max_requests: 100
    max_tokens: 200000
    deadline_seconds: 900
```

相同 `quota_group` 的连接在本次运行内共享 RPM/TPM 预留；连接并发限制也覆盖使用它的全部操作。这些控制不协调其他进程，也不能替代服务端的账号配额。Token 控制先按保守的提示词字节估算加显式输出上限预留，再根据返回的实际用量调整；预留量不是实际计费，也不是金额上限。启用 token 限制时，每个可达的主模型和备用模型都必须有有限输出上限。

`deadline_seconds` 和 Ctrl+C 协作式停止排队请求与重试等待；已进入 SDK 的请求仍可能执行到完成或连接超时。完成结果保留供续跑；重新启动会获得一份新的运行预算。

无状态请求可以显式设置 `fallbacks: [备用配置名]`。只有可重试的传输错误耗尽重试后才进入该链；认证、配置及输出结构错误不触发模型切换。可续跑的 `review.verify`、`review.arbitrate`、`autofix.verify` 对话禁止故障切换，避免一条取证轨迹混用模型。

### 用量与续跑

单一账本维护总量，以及互相独立的 `by_tier`、`by_stage`（操作 ID）、`by_provider`、`by_model` 视图。直接指定模型的调用归入 `direct` 档位。物理身份区分端点、模型与推理选项，即使复用了别名也不会混为一项；别名和显示标签不参与总量计算。解析失败、随后重试的响应仍保留实际用量；服务端没返回用量时不虚构 token 计费。

事件记录路由计划及请求的操作、模型、提供商、配置名、连接名、推理指纹、调用 ID、尝试次数。全书和 Review 账本先写 `usage-pending.json`，再更新正式账本；本地合并中断后能补完且不重复累计。若进程在远端受理后、本地保存前被强杀，仍可能存在无法确定的远端用量。

翻译、分析、概要、SRT 模型变更会保留完成结果，仅后续请求使用新路由。可达的 Review 模型、端点、选项或协议变化会开启新 Review；无关路由、密钥轮换、别名和并发调整不会使其失效。缺少推理身份的旧 Review 缓存保留供检查，但不复用。Autofix 使用独立指纹：已写发布索引的任务按保存的候选补完；未完成的模型规划需恢复原路由后继续。

旧配置和非空旧用量账本需要显式转换：

```bash
uv run trans-novel models migrate-config old-config.yaml --out routed-config.yaml
uv run trans-novel models migrate-usage state/BOOK/targets/zh
```

配置转换器生成独立文件；账本转换器逐份备份，保留总量及旧档位/阶段归属，将未知提供商和模型历史标记为 `unknown`，不会处理原书。转换账本前应停止该目标的运行任务。Review 目录保留。`pipeline.review_agent_tier` 由取证、仲裁、修订的独立路由取代。

`models compare --operation translation.body --model writer --model editor --messages fixture.json --out comparison.json` 会明确向每个模型发送由 `{role, content}` 对象构成的 JSON 消息数组，记录输出、延迟和实际用量。该命令消耗请求，不自动读取书籍或修改译文。选择混用配置前请用隔离的公版样本比较；支持路由不等于已经提供实测质量排序的新预设。

## 流水线

```yaml
pipeline:
  review: true
  polish: true
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  annotation_alignment: true
  annotation_alignment_concurrency: 4
  review_concurrency: 4
  review_output_retries: 2
  review_agent_loop: true
  review_agent_max_evidence_rounds: 2
  review_conflict_arbitration: true
  review_fix_loop: true
  review_fix_max_rounds: 2
  review_clean_confirmations: 2
  review_autofix: true
  glossary_scope: chapter
  pdf_backend: mineru
  babeldoc_bridge_url: http://127.0.0.1:8765
  babeldoc_timeout: 600
```

- `review`：默认开启；全书翻译完成时自动执行取证式全书审校。一键流程可用 `--no-review` 或设为 `false` 跳过。仍可显式调用 `trans-novel review`。
- `polish`：翻译后再调用强模型润色，质量可能提升，但显著增加耗时和成本。
- `rolling_context_segments`：每批翻译附带的前文译文段数。翻译与润色还会内置附带同章下一条原文片段作为只读参考，此值为零时也保留后文参考；它不改变输出段数，也不写入滚动译文上下文。详见[全书理解与上下文](pipeline.md#全书理解与上下文)。
- `book_understanding`：预扫全书，生成章节梗概和全书概览。
- `prescan_concurrency`：预扫章节梗概的并发数。
- `annotation_alignment`：默认开启。EPUB 中存在脚注、尾注等内部链接时，每个含注释的逻辑段在翻译和润色后立即针对正式译文串行调用一次模型定位。开启导出标点规范化时，导出层会在规范化内存副本的同时重映射已保存的偏移。超长续段会先重新合并，不含注释的段落不会调用模型。关闭后，译文侧仍保留链接但退化为段末可点击标记；未翻译原文及双语版原文侧保留源 EPUB 中的原始位置。该选项只控制链接定位；已经解析出的原语言注释正文始终会自动提供给对应翻译段落。
- `annotation_alignment_concurrency`：当一个逻辑段内注释数超过一条时，不再用一次模型调用要求同时摆对所有标记（一条出错就会连累整段全部标记回退），而是给每条注释单独发起一次并发请求；该项限制同一段内这些逐条请求可同时并发的上限。
- `review_concurrency`：针对同一份不可变译文快照执行连续审校块和同轮 Fixer 调用的并发上限；设为 `1` 时串行执行。
- `review_output_retries`：本地 JSON 修复和较大审校块拆分后，单段响应仍缺少有效完成回执时的额外重试次数；设为 `2` 表示连同初次调用最多尝试 3 次。
- `review_agent_loop`：原有 Reviewer 提示词在成功叶块中发现候选后，允许 Agent Loop 选择性请求证据，再确认、驳回或细化这些候选。
- `review_agent_max_evidence_rounds`：每个 Agent Loop 最多允许的选择性取证轮数，范围为 `0` 到 `2`；用完后必须给出最终结论。
- `review_conflict_arbitration`：所有块结束后，同一术语、人称或固定表达的一致性建议若互相矛盾，再执行只给建议、不修改数据的终局仲裁。
- `review_fix_loop`：针对确认的问题在本次运行的影子译文中生成完整单段替换，再从头盲审全书；关闭后保持单轮、只给建议的行为。
- `review_fix_max_rounds`：最多生成的临时 Fix 轮数，范围为 `0` 到 `4`；它不是 Review 总轮数。
- `review_clean_confirmations`：开启影子 Fix 后，需要连续无问题的全书 Review 次数，范围为 `1` 到 `2`，默认 `2`。
- `review_autofix`：默认开启。只读 Review 引擎结束后，先把折叠后的 `changes` 叠加到工作译文，再让每段剩余 issue 基于更新后的译文复用现有有界 Review Agent Loop，确认项继续交给现有 Review Fixer。可用 `--no-autofix` 或设为 `false`，避免写回正式 `target`。生成的完整单段译文只覆盖正式章节的 `target`，不修改 manifest 和术语库。完整前后版本链、issue ID、判定、失败原因和写回状态保存在本次 Review 的 `autofix/index.json`，不会给章节 JSON 新增历史字段。
- `glossary_scope`：`chapter` 仅带本章相关术语，`full` 带全量术语表。
- `pdf_backend`：默认 `mineru`，经 MinerU 转 HTML。需要尽量保留版式时改用 `babeldoc`（外部 AGPL HTTP bridge）。经 BabelDOC 创建的 PDF 状态，在 `translate` 和 `assemble` 中均默认导出 PDF；MinerU 状态仍默认导出 EPUB。显式 `--format` 优先，续跑默认格式以已保存的后端为准。
- `babeldoc_bridge_url`：BabelDOC bridge 地址，默认 `http://127.0.0.1:8765`。
- `babeldoc_timeout`：bridge extract / fillback 的 HTTP 超时秒数。
- `babeldoc_pages`：可选的 1-based 页码，如 `"15"` 或 `"6-8"`；省略则处理全书。

`translate` 命令的 `--polish`、`--no-polish`、`--review`、`--no-review`
会覆盖对应配置。

可使用 `trans-novel review INPUT` 独立执行最终审校。每次调用都会从头审查完整
译文。默认会在影子循环后发布折叠后的修订；可用 `--no-autofix` 保持本次只读，
或在配置关闭时用 `--autofix` 强制发布。Autofix 会先应用折叠后的 changes，再让最终未解决
issues 复用同一套 Agent Loop 和 Fixer，不会另建一套 Autofix loop 或 prompt。
统一结果和内部逐轮记录会保存到 `state/<书名>/targets/<目标语言>/reviews/review-<时间戳>/`。
本次 Review 用量既保存为目录内增量，也会计入本书累计用量。

## 输出

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  bilingual_preserve_source_style: false
  about_page: true
  punctuation_normalize: true
```

- `mono`：生成单语译本，文件名为 `<书名>.<目标语言>.<扩展名>`（通常为 `.zh.epub`，BabelDOC PDF 状态为 `.zh.pdf`，DOCX 输入为 `.zh.docx`）。
- `bilingual`：请求原文与译文对照版，文件名为 `<书名>.<目标语言>-bi.<扩展名>`，使用与单语输出相同的选定格式。
- `bilingual_order`：`target_first` 表示译文在上，`source_first` 表示原文在上。
- `bilingual_preserve_source_style`：设为 `true` 时，原文继承书籍正文样式，不使用灰色淡化背景；仅影响 EPUB 和 HTML。
- `about_page`：在书籍末尾附加“关于此翻译”项目说明页；设为 `false` 可关闭。
- `punctuation_normalize`：仅对简体中文目标的内存导出副本规范标点；繁体中文及其它目标语言跳过此机械转换。正式章节 `target`、Review 输入和续跑状态均保持不变。

旧的顶层 `punctuation.normalize` 配置不再接受；请删除旧配置，并只使用 `output.punctuation_normalize`。

默认只生成单语版；使用 `--bilingual` 可同时生成双语版，配置和命令行也可组合为仅生成双语版。

## 切分、敬称与路径

```yaml
segment:
  max_chars_per_batch: 1800
  max_chars_per_segment: 1200

honorific:
  strategy: keep_style

paths:
  state_dir: state
```

- `max_chars_per_batch`：单个模型翻译批次的目标字符数。
- `max_chars_per_segment`：超长段落的拆分阈值。
- `honorific.strategy`：日语源文本的敬称处理策略，可选 `keep_style`、`normalize`、`drop`。
- `state_dir`：书籍断点、章节产物、术语库、用量和报告的位置。字幕运行使用独立目录树 `<state_dir>/srt/<slug>/targets/<目标语言>/`（manifest、cues、batches、usage、events），不会创建术语库或审校目录。

所有书籍目标（包括默认 `zh`）统一使用 `<state_dir>/<slug>/targets/<目标语言>/`，字幕对应 `<state_dir>/srt/<slug>/targets/<目标语言>/`。每个目录有独立译文、术语、上下文、账本和 Review。不再查找或迁移旧版本保存在书名根目录下的状态。请使用当前配置重新开始翻译，原有文件保持不动。保存的 manifest 必须包含 `source_lang`、`target_lang` 和有效的 `source_sha256`。
