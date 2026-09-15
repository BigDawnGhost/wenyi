# 配置指南

[English](../configuration.md) · [使用指南](usage.md) · [Web 部署](../web.md)

Web 与 CLI 共用 `wenyi_core.config.Config`。CLI 读取 `--config` 指定文件（默认 `config.yaml`）；Web 从 `WENYI_CONFIG` 读取服务默认配置，应用所选流程，再合并项目保存的显式设置。Web 路径由服务管理，提供商凭证保留在环境变量中。

## 最小配置与默认值

```yaml
language:
  source: auto
  target: zh
llm:
  preset: deepseek
segment:
  max_tokens_per_batch: 1800
  max_tokens_per_segment: 1200
pipeline:
  book_understanding: true
  polish: true
  review: true
  review_autofix: true
output:
  mono: true
  bilingual: false
  punctuation_normalize: true
```

语言列表见 `trans-novel languages`。模型生成的说明性元数据使用目标语言，Web 界面维持中文。Web 项目初始化后不能改变目标语言或源文件内容身份。

Token 预算使用 `tiktoken` 的 `cl100k_base` 编码，控制原文分组和按句切分，不等同于模型输出上限。`max_tokens_per_segment` 取代旧字符预算字段。

## 提供商、模型配置和路由

内置注册表包含 DeepSeek、OpenAI、OpenRouter、OrcaRouter、Google Gemini、Ollama、vLLM、通用 OpenAI 兼容接口及测试用 Fake。预设提供 `providers`、`models`、`tiers`；也可全部显式定义。档位名称固定为 `strong`、`cheap`、`fast`。

例如保留 DeepSeek 预设，同时将初始审校路由到另一个提供商：

```yaml
llm:
  preset: deepseek
  providers:
    review_connection:
      kind: openai
      api_key_env: OPENAI_API_KEY
      timeout: 600
      max_retries: 4
      max_concurrency: 2
  models:
    review_model:
      provider: review_connection
      model: your-review-model
      max_output_tokens: 4096
  routes:
    review.scan:
      model: review_model
      fallbacks: [default_cheap]
```

把 `your-review-model` 替换成端点支持的模型。YAML 只保存密钥环境变量名。每条路由必须且只能选择一个 `model` 或 `tier`；`fallbacks` 是显式模型配置名列表。提供商专属请求参数放在 `models.<名称>.options`，由适配器校验。

覆盖预设中同名连接或模型时，该定义会整体替换，应提供完整定义。Web 项目覆盖按配置段和具名映射合并；切换预设时使用新预设及其显式覆盖，不沿用旧预设展开出的模型连接。

```bash
uv run trans-novel models list
uv run trans-novel models explain --operation translation.body
uv run trans-novel models check --workflow review
```

常见操作包括 `language.detect`、`analysis.style`、`synopsis.chapter`、`synopsis.book`、`translation.body`、`translation.title`、`polish.body`、`glossary.extract`、`annotation.align`、`review.scan`、`review.verify`、`autofix.verify`、`autofix.fix`、`srt.translate`。完整列表以注册表和 `/capabilities` 为准。

### 限制与预算

连接支持 `max_concurrency`、`max_retries`、`timeout` 和可选 `quota_group`。在 `llm.quotas` 定义配额组，在 `llm.budget` 定义单次执行预算：

```yaml
llm:
  preset: deepseek
  budget:
    max_requests: 2000
    max_tokens: 2000000
    deadline_seconds: 7200
```

配额组支持 `requests_per_minute` 与 `tokens_per_minute`。这些限制在当前客户端进程内执行，不是多个 Worker 共享的全账户分布式限流器。显式备用路由、提供商重试与成功请求用量进入操作记录。

## 书籍流程选项

| 字段 | 默认 | 作用 |
|---|---:|---|
| `pipeline.book_understanding` | `true` | 翻译前生成章节摘要和全书梗概。 |
| `pipeline.polish` | `true` | 润色并保留润色前译文。 |
| `pipeline.review` | `true` | 翻译后执行全书审校。 |
| `pipeline.review_autofix` | `true` | 将审校修订发布到正式译文。 |
| `pipeline.rolling_context_segments` | `6` | 提供最近已译段落作为上下文。 |
| `pipeline.prescan_concurrency` | `4` | 章节摘要并发数。 |
| `pipeline.annotation_alignment` | `true` | 在译文段落内放置 EPUB 注释链接。 |
| `pipeline.annotation_alignment_concurrency` | `4` | 每段多个注释的定位并发上限。 |
| `pipeline.align_retry_limit` | `2` | 翻译对齐失败后重试次数。 |
| `pipeline.glossary_scope` | `chapter` | 本章相关术语；`full` 提供全表。 |

Web 标准流程使用服务默认值；快速出稿关闭预理解、润色、审校和自动修复。自定义步骤或项目 YAML 可覆盖。项目运行期间，冲突的配置修改和人工正文修改会被拒绝。

### 审校选项

| `pipeline` 下的字段 | 默认 |
|---|---:|
| `review_concurrency` | `4` |
| `review_output_retries` | `2` |
| `review_agent_loop` | `true` |
| `review_agent_max_evidence_rounds` | `2` |
| `review_conflict_arbitration` | `true` |
| `review_fix_loop` | `true` |
| `review_fix_max_rounds` | `2` |
| `review_clean_confirmations` | `2` |

修订循环修改影子译文，`review_autofix` 控制独立发布阶段。仅看建议用 `--no-autofix` 或配置 `false`。已完成且匹配的审校结果可复用，匹配的中断运行可恢复；相关配置/模型路由、术语或译文变化后不复用旧结果。

## 输出、PDF 与路径

```yaml
pipeline:
  pdf_backend: mineru
  babeldoc_bridge_url: http://127.0.0.1:8765
  babeldoc_timeout: 600
  # babeldoc_pages: "6-8"
honorific:
  strategy: keep_style
paths:
  state_dir: state
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  bilingual_preserve_source_style: false
  about_page: true
  punctuation_normalize: true
```

`honorific.strategy` 可选 `keep_style`、`normalize`、`drop`，存在语言专属规则时会应用。双语顺序可选 `target_first` 或 `source_first`。

`punctuation_normalize` 只在适用目标语言的导出副本上操作，不回写模型译文。`paths.state_dir` 是 CLI 设置；Web 拒绝项目自行指定存储路径。

默认 MinerU 解析需要 `MINERU_API_KEY`。BabelDOC 作为独立 HTTP bridge 部署，通过 `pdf_backend: babeldoc` 选择。容器中应填 API 与两个 Worker 都可访问的主机名；`127.0.0.1` 指各自容器内部。已保存的解析后端信息决定后续是否使用 BabelDOC 回填 PDF。

fpdf2 输出要求字体包含 **TrueType 轮廓**（`glyf` 表）。导出器跳过不兼容的 OpenType/CFF 候选；显式配置不兼容字体时，在写 PDF 前返回错误。可安装 `fonts-wqy-zenhei`，或通过 `TRANS_NOVEL_PDF_FONT` 指定覆盖原文及译文字符的兼容 TTF/TTC 字体。Docker 为 fpdf2 内置 WenQuanYi Zen Hei；WeasyPrint 继续使用已安装的 Noto CJK 字体。不能仅按文件扩展名判断轮廓格式。

## 已移除的设置

独立一致性 QA、回译抽检、`autofix_severe`、`review_agent_tier`、旧 `force` 参数和字符预算字段已移除。标点规范化配置位于 `output`。配置校验会拒绝不支持的流水线/切分字段。此次 Web 更新使用全新数据库结构，不自动迁移旧策略或数据库。
