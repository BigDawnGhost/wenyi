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

## 模型

```yaml
llm:
  provider: deepseek
```

只需选择模型提供商。DeepSeek provider 默认使用：

- `https://api.deepseek.com`；
- `DEEPSEEK_API_KEY` 环境变量；
- `deepseek-v4-pro` 作为 strong 档；
- `deepseek-v4-flash` 作为 cheap 和 fast 档。

API Key 始终从环境变量读取，避免把密钥写进配置并提交到仓库。离线测试或调试可将 `provider` 改为 `fake`，此时不会发网络请求。

PDF 输入的首次解析另外读取 `MINERU_API_KEY`，用于调用 MinerU
转换服务。该密钥与 LLM provider 配置无关，也不写入 `config.yaml`。

需要代理、自定义环境变量或覆盖模型时，可添加高级配置：

```yaml
llm:
  provider: deepseek
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: deepseek-v4-pro
      options:
        reasoning_effort: high
        thinking: true
    cheap:
      model: deepseek-v4-flash
      options:
        reasoning_effort: high
        thinking: true
    fast:
      model: deepseek-v4-flash
      options:
        thinking: true
```

`max_retries` 表示由 Wenyi 统一执行的额外尝试次数。Provider SDK 的内置重试会被关闭，避免请求层层叠加；连接/超时、HTTP 408/409/429、5xx 瞬时错误以及模型空响应会重试，每次等待都会写入本书的 `events.jsonl`。

用户配置的档位会覆盖 provider 中对应的默认档位，未配置的档位继续使用默认值。
运行时若请求了仍不存在的档位，则按 `fast -> cheap -> strong` 回退。
`options` 由所选 provider 自行解释和校验；上述 `thinking`、`reasoning_effort`
只属于 DeepSeek，不会进入通用 LLM 抽象层。

### OpenAI 与 OpenRouter

OpenAI 和 OpenRouter 分别维护独立 provider，会自动选择各自的 Base URL、API Key
环境变量和思考参数格式。模型档位需要显式配置：

```yaml
llm:
  provider: openrouter
  tiers:
    strong:
      model: anthropic/claude-opus-4.6
      options:
        thinking: true
        reasoning_effort: high
    cheap:
      model: openai/gpt-5-mini
      options:
        thinking: true
        reasoning_effort: medium
    fast:
      model: google/gemini-3-flash
      options:
        thinking: false
```

`openai` 默认读取 `OPENAI_API_KEY`，`openrouter` 默认读取 `OPENROUTER_API_KEY`。两者均可使用 `base_url`、`api_key_env` 覆盖默认值。

### OrcaRouter

OrcaRouter 提供 OpenAI 兼容接口。内置 `orcarouter` provider 默认使用
`https://api.orcarouter.ai/v1`，并从 `ORCAROUTER_API_KEY` 环境变量读取密钥。
可先[创建 OrcaRouter API Key](https://api.orcarouter.ai/ref/ref_262c8b8e6a274286a90a)，
再配置当前账户可用的模型 ID：

```bash
export ORCAROUTER_API_KEY=sk-orca-...
```

```yaml
llm:
  provider: orcarouter
  tiers:
    strong:
      model: your-model-id
    cheap:
      model: your-cheap-model-id
    fast:
      model: your-fast-model-id
```

模型档位需要显式配置。OrcaRouter 使用下文的通用 OpenAI 兼容选项，包括
`reasoning_style` 和各档位的 `request_overrides`；需要时也可覆盖 `base_url` 或
`api_key_env`。

### Google Gemini

通过官方 `google-genai` SDK 原生支持 Google Gemini 模型，设置 `provider: gemini`（或 `provider: google`）。默认读取 `GEMINI_API_KEY`（或兼容 `GOOGLE_API_KEY`）环境变量：

```yaml
llm:
  provider: gemini
  api_key_env: GEMINI_API_KEY
  tiers:
    strong:
      model: gemini-3.6-flash
    cheap:
      model: gemini-3.6-flash
    fast:
      model: gemini-3.6-flash
```

Gemini 专属配置还支持针对思考模型的 `thinking_level`（如 `low` / `high`）与 `thinking_budget` 参数。

### 其他 OpenAI 兼容端点

任意兼容 Chat Completions 的端点可使用 `openai-compatible`：

```yaml
llm:
  provider: openai-compatible
  base_url: https://api.example.com/v1
  api_key_env: EXAMPLE_API_KEY
  # deepseek | openai | openrouter | none
  reasoning_style: deepseek
  tiers:
    strong:
      model: provider-model-name
      options:
        thinking: true
        reasoning_effort: high
        request_overrides:
          thinking:
            budget: 8192
```

`reasoning_style` 把统一的 `thinking`、`reasoning_effort` 转换为中转站实际
接受的请求格式：

- `deepseek`：`thinking.type` 与 `reasoning_effort`；
- `openai`：`reasoning_effort`，关闭时发送 `none`；
- `openrouter`：`reasoning.effort`，关闭时发送 `reasoning.enabled: false`；
- `none`：不转换，适合依赖模型默认行为或使用自定义请求字段。

默认情况下，Wenyi 只信任标准 `content` 字段，并对空响应发起重试。只有
确认端点会把最终 JSON 放进 `reasoning_content` 时，才应在实际使用的每个
档位设置 `json_response_fallback: reasoning_content`；启用后也只接受完整、
合法的单个 JSON 值。

```yaml
llm:
  provider: openai-compatible
  tiers:
    strong:
      model: provider-model-name
      options:
        json_response_fallback: reasoning_content
```

`request_overrides` 是未知中转协议的兜底入口，其内容会作为原始顶层请求体
字段发送，并在方言生成的字段之后递归合并。例如中转站使用
`enable_thinking: true` 时可以这样配置：

```yaml
llm:
  provider: openai-compatible
  base_url: https://api.example.com/v1
  reasoning_style: none
  tiers:
    strong:
      model: provider-model-name
      options:
        thinking: true
        request_overrides:
          enable_thinking: true
```

方言由中转站协议决定，而不是由实际模型名称决定。例如，中转站即使代理
DeepSeek 模型，只要它要求 OpenAI 的 `reasoning_effort` 格式，就应选择
`reasoning_style: openai`。

本地 Ollama 和 vLLM 还可以分别使用 `ollama`、`vllm`，默认地址为
`http://localhost:11434/v1` 和 `http://localhost:8000/v1`，默认不要求 API Key。
两者同样需要配置实际部署的模型档位。Ollama 的 OpenAI 兼容接口可使用
`reasoning_style: openai`；vLLM 是否支持思考开关取决于模型模板和服务端启动
参数，必要时可通过 `request_overrides.chat_template_kwargs` 传入
`enable_thinking`。

## 流水线

```yaml
pipeline:
  review: false
  polish: true
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  annotation_alignment: true
  annotation_alignment_concurrency: 4
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
- `rolling_context_segments`：每批翻译附带的前文译文段数。
- `book_understanding`：预扫全书，生成章节梗概和全书概览。
- `prescan_concurrency`：预扫章节梗概的并发数。
- `annotation_alignment`：默认开启。EPUB 中存在脚注、尾注等内部链接时，每个含注释的逻辑段在翻译、润色和标点定稿后立即串行调用一次模型定位；超长续段会先重新合并，不含注释的段落不会调用模型。关闭后，译文侧仍保留链接但退化为段末可点击标记；未翻译原文及双语版原文侧保留源 EPUB 中的原始位置。该选项只控制链接定位；已经解析出的原语言注释正文始终会自动提供给对应翻译段落。
- `annotation_alignment_concurrency`：当一个逻辑段内注释数超过一条时，不再用一次模型调用要求同时摆对所有标记（一条出错就会连累整段全部标记回退），而是给每条注释单独发起一次并发请求；该项限制同一段内这些逐条请求可同时并发的上限。
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
- `state_dir`：书籍断点、章节产物、术语库、用量和报告的位置。字幕运行使用独立目录树 `<state_dir>/srt/<slug>/`（manifest、cues、batches、usage、events），不会创建术语库或审校目录。
