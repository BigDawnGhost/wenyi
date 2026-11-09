# 配置参考

本页按设置与默认值提供查询，不描述界面布局或内部协议。
具体操作见 [CLI](../guide/cli.md)、[Desktop](../guide/desktop.md)、
[Web](../guide/web.md) 和[工作区](../guide/workspace.md)指南。
质量／成本取舍见[翻译流程](../guide/pipeline.md)，
格式兼容与输出限制见[格式参考](formats.md)。

<a id="scope"></a>

## 配置来源与作用范围

CLI 读取当前工作目录的 `config.yaml`；使用 `wenyi --config PATH ...` 选择其他文件。
缺少文件时会创建带注释的默认配置，帮助／版本命令也会如此。
省略的设置使用内置默认值。已保存设置含有不兼容字段时会[先备份再重建](#saved-settings-reconstruction)；
新提交到 Web/Desktop 的 API/YAML 设置，以及直接调用 Core 的配置校验，仍拒绝未知字段。

| 配置节 | 控制内容 |
|---|---|
| `language` | 源语言与目标语言。 |
| `llm` | 提供商连接、模型配置、档位、路由、配额和预算。 |
| `segment` | 源文分批与超长段落切分。 |
| `pipeline` | 准备、翻译、润色、Review 和 PDF 后端。 |
| `output` | 单／双语版本、双语样式、说明页和标点。 |
| `paths` | CLI 状态位置。 |

**平台边界**

- Web 从 `WENYI_CONFIG`（默认 `config.yaml`）初始化总设置；
  保存后的设置位于 PostgreSQL，不改写 CLI 文件。
- Desktop 从内置默认值初始化设置，保存到选定
  [数据根目录的目录库](../guide/desktop.md#数据位置与备份)，
  不读取 CLI 当前目录配置，也不导入 Web 项目。
- Web/Desktop 总设置管理连接、模型定义、预设与提供商配额。
  项目通过 `llm.tiers`、`llm.routes` 及备用路由选用已注册模型，
  可以设置 `llm.budget`，但不能定义私有模型注册库。自定义 ID 应先在总设置注册。
- 项目创建时复制默认值；之后修改全局默认值不会重置已有项目选择。
  模型库编辑影响新启动／恢复的任务；排队和运行中的任务保留其配置快照。
- Web/Desktop 的翻译方式与提示词题材在创建时确定；只要选择仍有效，其他设置或默认值变化不改变它们。
  项目保存的 `pipeline.prompt_genre` **不属于** Core/CLI YAML 或总设置字段；
  CLI 使用 [`--prompt-genre literary|academic`](../guide/cli.md#prompt-genres)
  选择本次调用的风格，默认 `literary`。
- 重命名注册 ID 会更新项目选择，历史用量和排队快照保留旧 ID。
  被引用的注册项必须先调整引用才能删除。

CLI/Web 的密钥只从环境变量读取。Desktop 另支持手动凭据：
系统凭据库可用时自动使用，否则只保存在当前会话内存中。
真实密钥不能进入 YAML、URL、模型选项或共享文件。
输入、保存和重置凭据的操作见各平台指南，尤其是 [Desktop](../guide/desktop.md)。

<a id="languages"></a>

## 语言

| 设置 | 默认值 | 取值／含义 |
|---|---|---|
| `language.source` | `auto` | 模型识别，或显式支持的代码。 |
| `language.target` | `zh` | 支持的代码，不接受 `auto`。 |

```yaml
language:
  source: auto
  target: zh
```

| 代码 | 语言 |
|---|---|
| `zh`、`zh-Hant` | 简体中文、繁体中文 |
| `en`、`en-US`、`en-GB` | 英语、美式英语、英式英语 |
| `ja`、`ko` | 日语、韩语 |
| `fr`、`de`、`es`、`it` | 法语、德语、西班牙语、意大利语 |
| `pt`、`pt-BR`、`pt-PT`、`ru` | 葡萄牙语、巴西／欧洲葡萄牙语、俄语 |
| `vi` | 越南语 |

`wenyi languages` 无需凭据即可列出内置语言。已注册别名包括
`zh-Hans` / `zh-CN` → `zh`、`zh-TW` → `zh-Hant`、
`ja-JP` → `ja`、`ko-KR` → `ko`、`vi-VN` → `vi`。
已注册的文字／地区变体保留，不截断为两字母。
不支持的代码，以及规范化／识别后源目标完全相同的语言会被拒绝。

翻译在所选语言间直接进行，不经中文中转。生成的说明性元数据要求使用目标语言，
原文名称与别名保留用于匹配。CLI 文案和提示词指令仍使用英语。
没有 `ui_locale` 或 `prompt_locale` YAML 字段，界面语言是独立的平台偏好。

更换目标语言会选择独立状态。准备、翻译、Review、导出和查看时使用相同目标配置。
源语言与保存状态冲突时拒绝续跑。显式源语言只省去检测，不能解决后续提供商／账号错误。
多语言质量仍属实验性；RTL 目标及全部格式／语言组合尚未认证。

内置语言策略不是可配置的操作覆盖。策略变化可能在续跑时重建受影响的分析，
保留已完成译文和术语。仅影响输出的变化只需重新导出。
开发诊断与策略资源见[语言策略](../development/pipeline.md#language-policies)。

<a id="models-and-operation-routing"></a>

## 模型与操作路由

连接描述请求发往哪里，模型配置（profile）描述一次模型请求，
档位和操作路由负责选择模型配置。档位是便捷角色，不是质量或价格实测排名。

<a id="presets"></a>

### 预设

```yaml
llm:
  preset: deepseek
```

省略或留空 `llm` 会使用 DeepSeek 预设。
每个预设展开为连接 `default`、模型配置 `default_strong`、
`default_cheap`、`default_fast` 和三个档位映射：

| 预设 | 三个档位的模型 | 模型选项 |
|---|---|---|
| `deepseek` | `deepseek-flash` | `thinking: true`、`reasoning_effort: high` |
| `gemini` | `gemini-3.6-flash` | 不设置 thinking／temperature，沿用服务默认。 |
| `fake` | `fake` | 离线测试适配器，无真实翻译、无需凭据。 |

其他提供商没有内置预设，需要显式配置模型。
预设不会向远端查询能力。

<a id="model-profiles"></a>

### 连接、模型配置与路由

| 设置 | 默认值／要求 |
|---|---|
| `llm.preset` | 可选；选择现有预设，或显式定义模型注册和全部档位。 |
| `llm.providers.<id>.kind` | 必填，已注册的提供商类型。 |
| `llm.providers.<id>.base_url` | 适配器默认值，或显式 HTTP(S) 绝对 URL；不能含凭据、query 或 fragment。 |
| `llm.providers.<id>.api_key_env` | 适配器默认环境变量名；保存名字而非密钥值。 |
| `llm.providers.<id>.timeout` | `600` 秒，正的有限数值。 |
| `llm.providers.<id>.max_retries` | 额外传输尝试 `4` 次，整数 ≥ 0。 |
| `llm.providers.<id>.max_concurrency` | 不设置则不限制；设置时为正整数。 |
| `llm.providers.<id>.quota_group` | 默认不设置，否则引用已有配额组。 |
| `llm.models.<id>.provider` | 必填连接 ID。 |
| `llm.models.<id>.model` | 必填远端模型名。 |
| `llm.models.<id>.max_output_tokens` | 默认不设置；设置时为正整数。 |
| `llm.models.<id>.options` | `{}`，下文的适配器专属字段。 |
| `llm.tiers` | 恰好包含 `strong`、`cheap`、`fast`，各自映射到模型配置。 |
| `llm.routes.<operation>.model` / `.tier` | 每条显式路由必须且只能选择一个；省略路由则使用操作默认值。 |
| `llm.routes.<operation>.fallbacks` | `[]`；按顺序填写模型配置 ID，受下文故障切换限制。 |
| `llm.quotas` | `{}`，可选命名配额组。 |
| `llm.budget` | 未设置则不限制。 |

连接、模型和配额 ID 以字母开头，仅包含字母、数字、下划线或连字符。
路由模型中的未知操作、字段和引用会在校验时失败。
三个档位可以全部指向同一模型，但不能缺失。

覆盖预设时按 ID **整体替换连接／模型配置**，需要重复必填字段。
模型选项不与旧配置合并。档位和路由按各自的键选择。

例如保留默认翻译路由，另选润色与取证模型：

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

把模型占位符替换成端点支持的模型。未覆盖时，`autofix.verify`
继承实际 `review.verify` 路由。
用 `wenyi models list`、`models explain --operation ID` 和
`models check --for prepare|translate|review` 本地检查。
只有 `models ping` 发起真实探测，详见 [CLI 命令](../guide/cli.md#model-commands)。

<a id="provider-options"></a>

### 提供商选项

| 类型 | 默认端点 | 默认凭据 |
|---|---|---|
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `opencode-go` | `https://opencode.ai/zen/go/v1` | `OPENCODE_API_KEY` |
| `gemini` | 原生 API，`https://generativelanguage.googleapis.com` | 未指定自定义变量时，先 `GEMINI_API_KEY`，再 `GOOGLE_API_KEY` |
| `openai-compatible` | 必须显式设置 `base_url` | 可选 `api_key_env` |
| `orcarouter` | `https://api.orcarouter.ai/v1` | `ORCAROUTER_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | 可选 `api_key_env` |
| `vllm` | `http://localhost:8000/v1` | 可选 `api_key_env` |
| `fake` | 无网络 | 无 |

| 模型选项 | 适用类型 | 默认值／约束 |
|---|---|---|
| `thinking` | DeepSeek、OpenAI、OpenRouter | `true` |
| `thinking` | OpenCode Go 与兼容类型 | `false` |
| `reasoning_effort` | 上述类型 | `high`；远端接受的值依端点／模型而定。 |
| `extra_body` | DeepSeek、OpenAI、OpenRouter、OpenCode Go、Gemini | `{}`，端点专属请求扩展。 |
| `thinking_level` | Gemini | 默认不设置，与 `thinking_budget` 互斥。 |
| `thinking_budget` | Gemini | 默认不设置，与 `thinking_level` 互斥。 |
| `temperature` | Gemini | 默认不设置。 |
| `json_response_fallback` | 兼容类型 | `none`；网关在该字段返回有效 JSON 时可显式选 `reasoning_content`。 |
| `request_overrides` | 兼容类型 | `{}`，端点专属请求扩展。 |

“兼容类型”指 `openai-compatible`、`orcarouter`、`ollama` 和 `vllm`。
它们还支持**连接级** `llm.providers.<id>.reasoning_style`：
`none`（默认）、`deepseek`、`openai` 或 `openrouter`。
`fake` 不接受模型选项。OpenCode Go 自动发送 Wenyi 身份／会话请求头。

DeepSeek 推理强度支持 `low`、`high` 和 `max`；
`thinking: false` 关闭思考并省略强度参数。
未显式设置输出上限时，DeepSeek 文档所列服务默认值为非思考 8K、
思考 64K、`max` 强度 128K；当前服务限制以
[请求参数](https://api-docs.deepseek.com/api/create-chat-completion/)为准。

流程操作不会暗中添加输出 token 上限，包括摘要与注释定位。
省略 `max_output_tokens` 时不传上限，使用服务默认值，不代表无限输出。
使用 OpenAI 兼容传输的适配器在 thinking 开启时拒绝低于 4096 的显式上限。
连通性探测使用独立的小预算。

原始扩展字典不能覆盖模型身份、消息、流式开关、JSON 模式、凭据、超时／重试或输出上限。
离线校验不能证明远端模型支持某个形式上有效的参数。

兼容适配器在任务请求中遇到端点明确拒绝初始 JSON 格式时会协商其他 JSON 格式。
没有需要保存的 JSON 模式偏好，也不会回退为无约束文本。
连通性测试使用纯文本，不证明结构化任务输出可用。

传输重试由 Wenyi 负责，SDK 内置重试关闭。
连接／超时、HTTP 408/409/429、5xx 和空响应可重试，普通 4xx 不重试。
重试等待会释放连接名额并响应取消；输出结构重试由流程设置独立控制。

MinerU PDF 解析独立于模型连接，CLI/Web 使用 `MINERU_API_KEY`。
Desktop 优先检查该环境变量，再使用手动凭据，见 [Desktop 凭据](../guide/desktop.md)。
只有未缓存的 MinerU 转换需要密钥，密钥不应进入项目配置或解析缓存。
BabelDOC 是独立 HTTP 服务。

<a id="registered-operations"></a>

### 已注册操作

以下是完整的当前路由 ID。即使阶段关闭，也可以配置其路由；
可达性检查按所选流程开关进行。

| 操作 | 默认档位／继承 | 用途 |
|---|---|---|
| `language.detect` | `cheap` | 源语言识别 |
| `analysis.style` | `strong` | 风格与人物语气分析 |
| `synopsis.chapter` | `fast` | 章节梗概 |
| `synopsis.book` | `fast` | 全书概览 |
| `translation.body` | `strong` | 正文翻译，包括精翻初稿 |
| `translation.title` | `strong` | 章节与目录标题 |
| `polish.body` | `strong` | 润色，包括精翻综合 |
| `glossary.extract` | `fast` | 术语抽取 |
| `glossary.align_history` | `fast` | 历史译法对齐 |
| `terminology.discover` | `strong` | 基于原文的人物发现 |
| `terminology.evidence` | `strong` | 全书原文取证 |
| `terminology.merge` | `strong` | 证据备注综合 |
| `annotation.align` | `cheap` | 注释定位 |
| `review.scan` | `cheap` | 初审与盲审复查 |
| `review.verify` | `strong` | 取证核查 |
| `review.arbitrate` | `strong` | 矛盾建议仲裁 |
| `review.fix` | `strong` | 本次运行内的影子修订 |
| `autofix.verify` | 已解析的 `review.verify` 路由 | 发布取证核查 |
| `autofix.fix` | 已解析的 `review.fix` 路由 | 发布修订 |

<a id="limits-and-failover"></a>

### 配额、预算与故障切换

| 设置 | 默认值 | 约束 |
|---|---|---|
| `llm.quotas.<id>.requests_per_minute` | 不设置 | 正整数 |
| `llm.quotas.<id>.tokens_per_minute` | 不设置 | 正整数 |
| `llm.budget.max_requests` | 不设置 | 正整数 |
| `llm.budget.max_tokens` | 不设置 | 正整数 |
| `llm.budget.deadline_seconds` | 不设置 | 正的有限秒数 |

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

此离线示例展示限额，不是生产翻译模型。
同一 `quota_group` 的连接在本次调用中共享预留；
连接并发上限覆盖使用该连接的全部操作。
这些控制不协调其他进程，也不能代替提供商账号限制。

Token 限额先按保守的提示词字节估算加配置的输出上限预留，
再根据实际返回用量调整。启用 token 限制时，
每个可达主模型和备用模型都必须有有限输出上限。
预留量不是计费量或金额上限。截止时间与 Ctrl+C 为协作式取消，
在途请求可能完成或超时；重新启动会获得新的一次调用预算。

允许故障切换的无状态操作可在显式路由中填写 `fallbacks: [backup_profile]`。
这些模型必须存在，不能与主模型或彼此重复。
只有可重试传输错误耗尽重试才切换，不因认证、配置或输出结构错误切换。
可续跑的 `review.verify`、`review.arbitrate` 和 `autofix.verify`
对话不允许故障切换。继承路由也会继承备用选择。

<a id="routing-and-resume"></a>

### 路由变更与续跑

更换翻译、分析或概览模型保留已完成工作，待完成请求使用新路由。
Review 身份匹配时可以复用或续跑；相关模型、端点、推理选项或协议变化会开启新 Review。
无关路由、凭据轮换、别名重命名与并发调整本身不会使它失效。
未完成的 Autofix 模型规划需要原路由；已经写入索引的发布可从保存的候选继续。

用量保持累计，并提供独立的档位、操作、提供商和模型视图；
直接选模型的请求归入 `direct` 档位。推理身份按模型／端点／选项区分，不只看别名。
解析失败与重试仍可能消耗 token，缺失的提供商计数不会被虚构。
用户操作见 [CLI 查看与迁移](../guide/cli.md#automation)，
账本内部机制见[流程开发文档](../development/pipeline.md)。

<a id="pipeline"></a>

## 流程设置

下表列出全部当前 Core `pipeline` 设置。
CLI 只覆盖其参数明确支持的设置；Web/Desktop 项目限制见[作用范围](#scope)。

<a id="translation"></a>

### 准备与翻译

| 设置 | 默认值 | 含义 |
|---|---|---|
| `pipeline.translation_mode` | `standard` | `standard` 或 `three_draft_synthesis`，后者要求 `polish: true`。 |
| `pipeline.polish` | `true` | 润色已译批次，增加较多模型工作。 |
| `pipeline.align_retry_limit` | `2` | 额外结构输出尝试次数；`0` 关闭。 |
| `pipeline.rolling_context_tokens` | `512` | 前文译文预算，整数 ≥ 0，按 `cl100k_base` 计数。 |
| `pipeline.book_understanding` | `true` | 生成覆盖完整原文的章节梗概和全书概览。 |
| `pipeline.prescan_concurrency` | `4` | 章节梗概并发；`1` 为顺序执行。 |
| `pipeline.terminology_context` | `false` | 仅 CLI 可用的实验性全书人物证据。 |
| `pipeline.annotation_alignment` | `true` | 定位译文链接／注释。EPUB 使用段末回退标记；PDF 忽略失败项，见 [PDF 格式说明](formats.md#babeldoc)。 |
| `pipeline.annotation_alignment_concurrency` | `4` | 单个逻辑段内逐注释请求的并发数。 |

`three_draft_synthesis` 是**三份独立初稿加一次对照原文的综合润色**，不是选一份胜出稿。
它使用 `translation.body` 和 `polish.body`；
正常批次四次调用，标准翻译加润色为两次。三稿并发是内置行为，不是用户设置。
保留相同模式／配置才能复用检查点。结构重试耗尽时精翻暂停，不自动拆段；
标准翻译可以回退到逐段请求。重试也产生用量。

滚动上下文选取预算内最近的连续译文，包含段间换行；
最近一段单独超限时，截取不破坏 Unicode 的尾部。
零只取消前文译文上下文，不取消内置的后文原文参考。
上下文从已保存译文重建。

`book_understanding` 增加首次预扫工作，长章节尤甚。
有原文的章节需要可用梗概才能翻译；全书概览失败不阻止翻译。
失败梗概可在下次 prepare/translate 中补齐。
无论是否开启 `terminology_context`，准备阶段都不抽取或预填术语。

术语证据在 CLI 待译工作中增加人物发现和原文备注，
其他术语类型保持常规抽取，不产生多义项或逐次出现的译名选择。
Web/API 不会仅因该 YAML 开关启用而生产证据。
Review 可以使用已有备注，不生成新证据。

正文翻译和新的 Reviewer 请求总是使用全量术语，没有范围选择器。
注释定位可能增加模型请求，定位失败的处理取决于[格式](formats.md)。
完整流程、成本和续跑语义见[流程指南](../guide/pipeline.md)，
标记／排版限制见[格式参考](formats.md)。

<a id="review"></a>

### Review 与发布

| 设置 | 默认值 | 含义／范围 |
|---|---|---|
| `pipeline.review` | `true` | 完整翻译后执行全书 Review。 |
| `pipeline.review_concurrency` | `4` | Review 块及同轮 Fixer 并发；`1` 为顺序执行。 |
| `pipeline.review_output_retries` | `2` | 单段 Review 输出无效的额外尝试次数；`0`–`5`。 |
| `pipeline.review_agent_loop` | `true` | 通过选择性取证核查初始候选。 |
| `pipeline.review_agent_max_evidence_rounds` | `2` | 每个循环取证轮数；`0`–`2`。 |
| `pipeline.review_conflict_arbitration` | `true` | 取证审校后仲裁矛盾的一致性建议。 |
| `pipeline.review_fix_loop` | `true` | 修订本次运行的影子译文，再盲审复查。 |
| `pipeline.review_fix_max_rounds` | `2` | 临时 Fix 轮数；`0`–`4`，不是 Review 总次数。 |
| `pipeline.review_clean_confirmations` | `2` | 连续无问题的全书确认次数；`1`–`2`。 |
| `pipeline.review_autofix` | `true` | 发布 Review 修订到正式章节译文。 |

**Review 默认可能修改正式译文。** 影子修订与发布是两步：
关闭 `review_fix_loop` 不等于关闭 Autofix。
只生成报告、不启动新的正式写回时，设置 `pipeline.review_autofix: false`，
或执行 `wenyi review INPUT --no-autofix`。
`--autofix` 可在 YAML 关闭时强制发布，不传这两个参数则继承 YAML。
已经保存 `applying` 记录的 Autofix 发布会在检查此开关前恢复，
因此关闭它不会取消待恢复的写回。

Autofix 发布折叠后的 Review 修订，并可对更新后的文本核查、修订剩余问题。
它修改章节译文，不修改术语库。采用结果前应阅读统一结果与发布记录。
内容、Review 配置／模型身份、指导和术语匹配时可复用或续跑，
不是每次都新建时间戳目录。

`translate --polish/--no-polish` 和 `--review/--no-review` 覆盖对应设置。
`translate` 没有 `--no-autofix`：可用 YAML 阻止自动 Review 启动新发布，
或跳过自动 Review，随后使用带 `--no-autofix` 的独立命令。
详见 [Review 与 Autofix](../guide/pipeline.md#review-and-autofix)。

<a id="pdf"></a>

### PDF 后端

| 设置 | 默认值 | 含义 |
|---|---|---|
| `pipeline.pdf_backend` | `mineru` | `mineru` 或 `babeldoc`。 |
| `pipeline.babeldoc_bridge_url` | `http://127.0.0.1:8765` | 外部 BabelDOC HTTP bridge。 |
| `pipeline.babeldoc_timeout` | `600` | 抽取／回填 HTTP 超时秒数。 |
| `pipeline.babeldoc_pages` | 不设置（`null`） | 可选的 1-based 页码，如 `"15"`、`"6-8"`；不设置即全书。 |

MinerU 将 PDF 转成 HTML，未缓存的导入需要单独凭据。
BabelDOC 是可选的独立 AGPL 服务，用于尽量保留 PDF 版式。
导出资格与默认格式由保存的解析身份决定；修改当前设置不会转换已有项目的后端。
选择后端时请查阅 [PDF 要求与限制](formats.md#pdf)，而不是模型路由表。

<a id="output"></a>

## 输出

| 设置 | 默认值 | 含义 |
|---|---|---|
| `output.mono` | `true` | 生成单语版。 |
| `output.bilingual` | `false` | 另生成原文／译文对照版。 |
| `output.bilingual_order` | `target_first` | `target_first` 或 `source_first`；选择按段配对格式中在前的版本，以及 PDF 并列页中位于左侧的版本。 |
| `output.bilingual_preserve_source_style` | `false` | 保留已记录的原文样式，替代该格式的默认原文显示方式。 |
| `output.about_page` | `true` | 为 EPUB 添加“关于此翻译”项目说明页。 |
| `output.punctuation_normalize` | `true` | 只在导出副本上规范简体中文标点。 |

`bilingual_preserve_source_style` 与顺序独立。
EPUB/HTML 中，`true` 用正常书籍样式替代淡化原文。
DOCX 中，`false` 使用正常正文样式；`true` 恢复已记录的字符样式，
包括段落／表格中的混合样式，无法恢复从未记录的样式。

标点规范化不修改正式章节译文或 Review 输入。
繁体中文和其他目标跳过该简体中文转换。
EPUB 说明页在简体中文目标下为中文，其余目标目前使用英语。

CLI 的 `translate` 和 `assemble` 可用 `--mono/--no-mono`、
`--bilingual/--no-bilingual` 覆盖输出默认值。
默认 YAML 下，`--bilingual` 生成两版，`--no-mono --bilingual` 只请求双语版。
格式与输出路径属于命令选项，不是 `output.format` 或 `output.path` 设置。
命名与可用性见[格式参考](formats.md)。

<a id="segmentation"></a>

## 切分

| 设置 | 默认值 | 含义／范围 |
|---|---|---|
| `segment.max_tokens_per_batch` | `1800` | 每个翻译批次的原文 token 预算；整数 ≥ 1。 |
| `segment.max_tokens_per_segment` | `1200` | 超长原文段落切分阈值；整数 ≥ 0，`0` 关闭切分。 |

两者使用 tiktoken `cl100k_base` 通用估算，不是线上提供商的实际分词器。
这是源文打包预算，不是模型输出 token 上限。

<a id="honorifics"></a>

## 敬称

敬称处理自动跟随源语言／目标语言策略，不提供独立配置项。
语言对专属规则优先于共享规则，以目标语言自然的表达保留人物关系与语气。
例如日译中规则包含 `先輩 → 前辈` 等处理。配置中不再有 `honorific` 节。

<a id="paths"></a>

## 路径与保存状态

`paths.state_dir` 默认 `state`，相对路径按 CLI 当前工作目录解释。
各目标使用 `<state_dir>/<slug>/targets/<目标语言>/`，
各自拥有译文、术语、用量、计时和 Review 数据。

续跑时保持相同目录、源内容和目标配置。
独立对照或重新翻译应选新目录，不要覆盖已有工作。
旧版书名根目录状态不会自动查找或迁移。
状态需要有效的源／目标语言和原文内容哈希，见 [CLI 续跑](../guide/cli.md#resume)。

<a id="saved-settings-reconstruction"></a>

## 已保存设置重建

CLI 加载已保存的 YAML，或 Web/Desktop 启动并读取全局、项目设置时，
会按当前 schema 重建不兼容配置：

1. 替换前先保存原设置备份。
2. 保留有效字段和值；丢弃已移除字段、无效值及无法继续校验的依赖选择。
   缺失字段使用当前默认值，项目模型选择按全局模型库校验。
3. 不将旧名称转换为兼容别名。不支持的值回到当前默认值；
   在创建项目或配置 CLI 运行时显式选择支持的选项。

Web/Desktop 也会先备份再重置不支持的默认流程模板和无效的已存项目流程选择。
有效的项目显式设置继续保留；重置不会复现已废弃流程的行为。

CLI 在所选文件旁保存原始字节备份，名称为 `<文件名>.backup-<sha256>.yaml`；
注释保留在备份里，重建后的 YAML 只保存支持的设置。
Web 备份保存在 PostgreSQL，Desktop 备份保存在所选数据根目录的目录库，
与各自设置一起保存。Web 遇到不兼容的初始 YAML 时也在 PostgreSQL 备份，
不改写挂载文件。有效设置不重写，重复启动不重复生成备份。

备份或替换保存失败时，原设置保持不变，加载／启动报错。
语法损坏的 YAML 需要修正，不猜测其含义。
重建不修改原书、章节译文、用量或冻结任务快照。
若排队任务仍带有不兼容的旧快照，需要按重建后的设置重新启动任务，
不会静默换一种模式执行。Web/Desktop 新设置提交与 Core 直接校验仍然严格。

可从备份取回当前 schema 仍支持的自定义选择，不再提供单独的配置转换命令。

用量账本属于运行数据，不属于设置；其转换和缓存计量规则见
[CLI 用量账本](../guide/cli.md#usage-ledgers)。
