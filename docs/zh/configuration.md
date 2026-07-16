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
        thinking: false
```

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

`openai` 默认读取 `OPENAI_API_KEY`，`openrouter` 默认读取
`OPENROUTER_API_KEY`。两者均可使用 `base_url`、`api_key_env` 覆盖默认值。

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
  review: true
  autofix_severe: false
  polish: true
  backtranslate_sample: 0
  consistency_qa: false
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  review_concurrency: 4
  glossary_scope: chapter
```

- `review`：每章翻译结束后检查漏译、误译、术语和人称问题。
- `autofix_severe`：自动重译并采纳通过校验的漏译、误译等严重问题。
- `polish`：翻译后再调用强模型润色，质量可能提升，但显著增加耗时和成本。
- `backtranslate_sample`：回译抽检比例，`0` 为关闭。
- `consistency_qa`：全书完成后进行跨章术语、人称、语气和标点检查。
- `rolling_context_segments`：每批翻译附带的前文译文段数。
- `book_understanding`：预扫全书，生成章节梗概和全书概览。
- `prescan_concurrency`：预扫章节梗概的并发数。
- `review_concurrency`：章末审校分块的并发数；设为 `1` 时串行审校。
- `glossary_scope`：`chapter` 仅带本章相关术语，`full` 带全量术语表。

命令行的 `--polish`、`--no-polish`、`--qa`、`--no-qa` 会覆盖对应配置。

## 输出

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  about_page: true
```

- `mono`：生成单语中文版，文件名为 `<书名>.zh.epub`。
- `bilingual`：生成原文与译文对照版，文件名为 `<书名>.zh-bi.epub`。
- `bilingual_order`：`target_first` 表示译文在上，`source_first` 表示原文在上。
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
