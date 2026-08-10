# 配置说明

[English](../configuration.md)

程序读取当前工作目录的 `config.yaml`。配置文件不存在时会自动创建带注释的默认文件。

## 语言

```yaml
language:
  source: auto
  target: zh
```

`source: auto` 会调用模型识别源语言；也可以写死 ISO 639-1 代码，例如 `ja`、`en`、`ko`、`ru`、`fr`、`de`、`es`。目标语言目前为简体中文。

## LLM API

文译只保留一个通用生产客户端，通过两种文本协议访问模型：Anthropic Messages
或 OpenAI Chat Completions。真实模型流程需要四项信息：

```yaml
llm:
  # anthropic | openai；大小写不敏感，也接受简写 a | oai
  api_format: openai

  # 二选一；同时出现时 api_key 优先
  api_key_env: LLM_API_KEY
  # api_key: sk-...

  # 可填 SDK 基础地址，也可填完整操作地址
  base_url: https://api.example.com/v1/chat/completions
  model: provider-model-name
```

直接填写的 `api_key` 会作为密文保存，配置对象展示时不会显示明文；仍建议优先
使用环境变量，以免误把密钥提交到仓库。`api_format: fake` 仅供离线测试，不会
发送网络请求。

PDF 输入首次解析会另外读取 `MINERU_API_KEY`，用于调用 MinerU 转换服务；它与
LLM API 配置互相独立。

只有真正构建模型客户端时才校验必填项。因此，默认配置里的 `base_url`、`model`
尚未填写时，`--help`、`assemble`、`report` 等无需模型的命令仍可正常使用；模型
流程会在第一次请求前一次性报告所有缺项。

### 可选请求参数与模型档位

其余 LLM 字段均可省略。全局值同时用于 `strong`、`cheap`、`fast` 三档；某档只
覆盖自己明确写出的字段：

```yaml
llm:
  api_format: openai
  api_key_env: LLM_API_KEY
  base_url: https://api.example.com/v1
  model: provider-model-name

  timeout: 600
  max_retries: 4
  max_tokens: 8192
  max_tokens_field: max_tokens # OpenAI 可选 max_tokens | max_completion_tokens
  temperature: 0.2
  thinking: true
  reasoning_effort: high
  request_overrides:
    metadata:
      application: wenyi

  tiers:
    fast:
      model: provider-fast-model
      thinking: false
      request_overrides:
        metadata:
          workload: prescan
```

档位之间不再回退模型。上例中 `strong`、`cheap` 使用全局模型，只有 `fast` 使用
自己的模型。合法档位名称只有 `strong`、`cheap`、`fast`；拼错或未知档位会明确
报错，不会静默改用全局模型。全局与档位的 `request_overrides` 会递归合并，所以 fast 请求会同时
得到 `metadata.application` 和 `metadata.workload`。

`request_overrides` 用于中转站或厂商特有的原始请求字段，但不能改写客户端维护的
`model`、`messages`、`stream`、凭据、输出 token 上限字段，以及 Anthropic 顶层
`system` 等结构。显式调用参数、JSON 模式和调用方给出的 `max_tokens` 最终优先。

`max_retries` 是文译统一管理的额外尝试次数；两个 SDK 的内置重试均被关闭。连接、
超时、HTTP 408/409/429 与 5xx 等瞬时故障才会重试，请求活动和每次等待都会写入
本书的 `events.jsonl`。服务端给出的有效 `Retry-After`、`retry-after-ms` 会被完整
遵守；只有文译自身的指数退避等待上限为 30 秒。

### Base URL 规范化

`base_url` 必须是 HTTP(S) URL，不能带 query 或 fragment。基础地址和完整标准
操作地址都可直接填写：

- OpenAI 格式会先剥离末尾的 `/chat/completions` 再交给 SDK；
- Anthropic 格式会先剥离末尾的 `/v1/messages`；
- 其他自定义路径前缀原样保留，只移除末尾 `/`。

例如 `https://api.example.com/v1` 与
`https://api.example.com/v1/chat/completions` 会访问同一个 OpenAI 端点。

### OpenAI Chat Completions 格式

OpenAI 分支保留 system、user、assistant 消息并调用 `chat.completions.create`。
JSON 模式会在提示词中加入 JSON 约束，同时发送
`response_format: {type: json_object}`。`thinking: true` 会发送配置的
`reasoning_effort`（未配置时为 `high`），`thinking: false` 则发送 `none`。

输出上限默认使用兼容面更广的 `max_tokens`。端点或新版 OpenAI 模型要求新字段
时，可设置 `max_tokens_field: max_completion_tokens`。

常见的 OpenAI 格式配置如下：

```yaml
# OpenAI
llm:
  api_format: openai
  api_key_env: OPENAI_API_KEY
  base_url: https://api.openai.com/v1
  model: your-openai-model

# Google Gemini 的 OpenAI 兼容端点
llm:
  api_format: openai
  api_key_env: GEMINI_API_KEY
  base_url: https://generativelanguage.googleapis.com/v1beta/openai
  model: your-gemini-model

# OpenRouter（DeepSeek 和其他中转站使用相同结构）
llm:
  api_format: openai
  api_key_env: OPENROUTER_API_KEY
  base_url: https://openrouter.ai/api/v1
  model: provider/model-name

# 本地 Ollama；若服务端不鉴权，可填写任意非空 key
llm:
  api_format: openai
  api_key: local
  base_url: http://localhost:11434/v1
  model: installed-model-name
```

本地 vLLM 的常见地址为 `http://localhost:8000/v1`，DeepSeek API 基础地址为
`https://api.deepseek.com`。文译不再内置任何厂商 URL、模型名或密钥环境变量名。

### Anthropic Messages 格式

Anthropic 分支会把所有 system 消息合并到顶层 `system`，其余 user/assistant 消息
保持顺序。JSON 模式只通过提示词约束，不发送 OpenAI 专属的 `response_format`。
若没有配置输出上限，Anthropic 请求默认使用 `max_tokens: 8192`。

`thinking: true` 映射为 adaptive thinking，`thinking: false` 映射为 disabled；
`reasoning_effort` 映射到 `output_config.effort`。旧模型若要求固定思考预算，可用
完整的厂商字段替换自动生成值：

```yaml
llm:
  api_format: anthropic
  api_key_env: ANTHROPIC_API_KEY
  base_url: https://api.anthropic.com
  model: your-claude-model
  request_overrides:
    thinking:
      type: enabled
      budget_tokens: 8192
```

响应只会把最终 text block 拼接给翻译流水线，thinking、tool 等块会被忽略。
Anthropic 的缓存写入/读取 token 与 OpenAI 的缓存 prompt token 都会归一到现有用量
统计中。

### 从旧 Provider 配置迁移

默认情况下，文译只信任标准 `content` 字段，并对空响应发起重试。只有确认
OpenAI 格式端点会把最终 JSON 放进 `reasoning_content` 时，才应在全局或实际
使用的档位设置 `json_response_fallback: reasoning_content`。备用字段只会在
JSON 模式读取，而且必须完整构成一个合法 JSON 值。

```yaml
llm:
  api_format: openai
  base_url: https://api.example.com/v1
  model: provider-model-name
  tiers:
    strong:
      json_response_fallback: reasoning_content
```

原 `llm.provider`、`reasoning_style`、`tiers.*.options` 不是兼容别名；发现它们时
文译会直接给出迁移示例。请改用 `api_format`、明确的 `base_url`、全局 `model` 和
上面的扁平可选字段，只有真正的厂商扩展才放入 `request_overrides`。

## 流水线

```yaml
pipeline:
  review: false
  polish: true
  backtranslate_sample: 0
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  annotation_alignment: true
  review_concurrency: 4
  review_output_retries: 2
  review_agent_loop: true
  review_agent_tier: strong
  review_agent_max_evidence_rounds: 2
  review_conflict_arbitration: true
  review_fix_loop: true
  review_fix_max_rounds: 2
  review_clean_confirmations: 2
  glossary_scope: chapter
```

- `review`：默认关闭；开启后在全书翻译完成时自动执行取证式全书审校。关闭时仍可显式调用 `trans-novel review`。
- `polish`：翻译后再调用强模型润色，质量可能提升，但显著增加耗时和成本。
- `backtranslate_sample`：回译抽检比例，`0` 为关闭。
- `rolling_context_segments`：每批翻译附带的前文译文段数。
- `book_understanding`：预扫全书，生成章节梗概和全书概览。
- `prescan_concurrency`：预扫章节梗概的并发数。
- `annotation_alignment`：默认开启。EPUB 中存在脚注、尾注等内部链接时，每个含注释的逻辑段在翻译、润色和标点定稿后立即串行调用一次模型定位；超长续段会先重新合并，不含注释的段落不会调用模型。关闭后，译文侧仍保留链接但退化为段末可点击标记；未翻译原文及双语版原文侧保留源 EPUB 中的原始位置。该选项只控制链接定位；已经解析出的原语言注释正文始终会自动提供给对应翻译段落。
- `review_concurrency`：针对同一份不可变译文快照执行连续审校块和同轮 Fixer 调用的并发上限；设为 `1` 时串行执行。
- `review_output_retries`：本地 JSON 修复和较大审校块拆分后，单段响应仍缺少有效完成回执时的额外重试次数；设为 `2` 表示连同初次调用最多尝试 3 次。
- `review_agent_loop`：原有 Reviewer 提示词在成功叶块中发现候选后，允许 Agent Loop 选择性请求证据，再确认、驳回或细化这些候选。
- `review_agent_tier`：取证循环、跨块仲裁和临时 Review Fixer 所用的模型档位，默认 `strong`。
- `review_agent_max_evidence_rounds`：每个 Agent Loop 最多允许的选择性取证轮数，范围为 `0` 到 `2`；用完后必须给出最终结论。
- `review_conflict_arbitration`：所有块结束后，同一术语、人称或固定表达的一致性建议若互相矛盾，再执行只给建议、不修改数据的终局仲裁。
- `review_fix_loop`：针对确认的问题在本次运行的影子译文中生成完整单段替换，再从头盲审全书；关闭后保持单轮、只给建议的行为。
- `review_fix_max_rounds`：最多生成的临时 Fix 轮数，范围为 `0` 到 `4`；它不是 Review 总轮数。
- `review_clean_confirmations`：开启影子 Fix 后，需要连续无问题的全书 Review 次数，范围为 `1` 到 `2`，默认 `2`。
- `glossary_scope`：`chapter` 仅带本章相关术语，`full` 带全量术语表。

`translate` 命令的 `--polish`、`--no-polish`、`--review`、`--no-review`
会覆盖对应配置。

可使用 `trans-novel review INPUT` 独立执行最终审校。每次调用都会从头审查完整
译文。Review 只会修改本次运行的影子译文，不会把替换写入正式翻译状态；统一
结果和内部逐轮记录会保存到 `state/<书名>/reviews/review-<时间戳>/`。
本次 Review 用量既保存为目录内增量，也会计入本书累计用量。

## 输出

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  bilingual_preserve_source_style: false
  about_page: true
```

- `mono`：生成单语中文版，文件名为 `<书名>.zh.epub`。
- `bilingual`：生成原文与译文对照版，文件名为 `<书名>.zh-bi.epub`。
- `bilingual_order`：`target_first` 表示译文在上，`source_first` 表示原文在上。
- `bilingual_preserve_source_style`：设为 `true` 时，原文继承书籍正文样式，不使用灰色淡化背景；仅影响 EPUB 和 HTML。
- `about_page`：在书籍末尾附加“关于此翻译”项目说明页；设为 `false` 可关闭。

默认只生成单语版；使用 `--bilingual` 可同时生成双语版，配置和命令行也可组合为仅生成双语版。

## 切分、敬称与路径

```yaml
segment:
  max_chars_per_batch: 1800
  max_chars_per_segment: 1200

honorific:
  strategy: keep_style

punctuation:
  normalize: true

paths:
  state_dir: state
```

- `max_chars_per_batch`：单个模型翻译批次的目标字符数。
- `max_chars_per_segment`：超长段落的拆分阈值。
- `honorific.strategy`：日语源文本的敬称处理策略，可选 `keep_style`、`normalize`、`drop`。
- `punctuation.normalize`：统一简体中文大陆常用全角标点。
- `state_dir`：断点、章节产物、术语库和报告的位置。
