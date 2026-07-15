# 更新日志

本文件记录文译（Wenyi）的重要版本变化。

## 0.2.0 - 2026-07-14

0.2.0 是一次功能与架构并重的版本更新。该版本扩展了模型提供商支持，完善了
单语和双语书籍输出，提高了长篇翻译的续跑效率，并加入了按书持久化的 Token
用量统计。

### 主要更新

- 重构 LLM 层，将通用接口、档位解析、JSON 解析和各模型提供商实现拆分维护。
- 新增 OpenAI、OpenRouter、OpenAI 兼容接口、Ollama 和 vLLM 支持，继续支持
  DeepSeek 与离线 Fake provider。
- 支持 DeepSeek、OpenAI 和 OpenRouter 不同的推理参数格式；通用兼容接口可通过
  `reasoning_style` 和档位 `options` 适配中转站或私有服务的特殊参数。
- 单语译本默认输出到源文件目录下的 `output/`；可选生成双语 EPUB，并配置原文
  与译文的排列顺序。
- 可在 EPUB 末尾附加“关于此翻译”说明页，默认开启，可通过
  `output.about_page` 关闭。
- 每本书在状态目录中增量维护 `usage.json`，按模型档位和流水线阶段统计输入、
  输出、缓存命中与总 Token；续跑时继续累计。
- 预处理、全书理解和审校阶段提供更细化的进度显示；章末审校支持并行执行。
- 续跑时根据已完成段落恢复全书进度，不再从零显示；已完成批次的术语提取通过
  事件检查点恢复，避免重复调用模型。

### 输出与排版

- 新增单语、双语输出开关，默认只生成单语译本。
- 双语版支持 `target_first` 和 `source_first` 两种排列方式。
- 修复列表和引用块在双语 EPUB 中被拆出原容器的问题。
- EPUB 输入继续保留原有样式、图片、目录、锚点和 XHTML 模板结构。
- 报告文件不再重复保存 Token 用量，`usage.json` 成为用量统计的唯一数据源。

### 模型提供商与请求兼容

- DeepSeek provider 内置默认服务地址、API Key 环境变量和三档模型配置。
- provider 专属参数移入档位的 `options`，避免通用配置模型被单一提供商字段污染。
- `thinking: false` 会向支持的提供商显式发送关闭推理的参数，避免模型采用服务端
  默认思考模式。
- OpenAI 请求使用 `max_completion_tokens`，兼容推理模型和新版 SDK。
- JSON mode 请求会明确在提示中包含 `json`，兼容要求提示词声明 JSON 输出的服务。
- 统一读取 DeepSeek 顶层缓存统计与 OpenAI 风格的
  `prompt_tokens_details.cached_tokens`。
- 通用 OpenAI 兼容 provider 支持 `request_overrides`，便于传递中转站或私有模型的
  扩展请求字段。

### 翻译流水线与续跑

- 预扫逐章梗概和章末审校均支持可配置并发。
- 全书概览、逐章梗概、风格分析等准备阶段使用独立进度显示。
- 修复阶段切换时进度条沿用上一步计数的问题。
- 续跑进度包含已经翻译的段落，并以全书可翻译段落数量作为总数。
- 为批次术语提取增加持久化完成事件；进程中断后可从未完成批次继续。
- 用量数据在每次模型调用后增量持久化，降低异常退出时统计丢失的风险。

### 文档解析与术语库

- FB2 的 `<body><title>` 作为独立可见章节保留，不再丢失作者或书名页。
- 支持缺少标准命名空间或使用不同命名空间形式的 FB2 文件。
- 移除未参与实际工作流的术语 `confidence` 和 `locked` 字段。
- 打开旧术语数据库时会自动迁移表结构并保留已有术语内容。
- 术语冲突保留当前译法，同时记录候选译法供人工检查。

### 配置与默认行为

- 默认生成单语版；双语版需要通过配置或命令行显式开启。
- 默认输出目录改为源文件旁的 `output/`。
- 配置文件、Pydantic 默认值和文档中的流水线开关已统一。
- 项目声明支持 Python 3.10 及以上版本。
- 新增和扩充配置、使用方法、流水线及双语输出文档。

### 升级注意事项

#### LLM 档位配置格式

0.1.1 将 provider 专属字段直接放在档位下：

```yaml
llm:
  tiers:
    strong:
      model: deepseek-v4-pro
      thinking: true
      reasoning_effort: high
```

0.2.0 将这些字段移入 `options`：

```yaml
llm:
  tiers:
    strong:
      model: deepseek-v4-pro
      options:
        thinking: true
        reasoning_effort: high
```

继续使用旧格式会触发配置校验错误。升级前请参照 `config.yaml` 或
`docs/configuration.md` 调整现有配置。

#### Python 导入路径

LLM provider 已从原来的单文件实现拆分到 `trans_novel.llm.providers`。命令行用户
不受影响；直接导入旧内部类的第三方代码需要改用新的 provider 模块或
`trans_novel.llm.factory.build_client`。

#### 状态与统计文件

- 旧章节状态可以继续用于断点续跑。
- 旧术语数据库会自动迁移，仍建议在首次使用 0.2.0 前备份对应书籍的状态目录。
- Token 用量仅以状态目录中的 `usage.json` 为准，`report.json` 不再包含重复副本。

### 验证

- 完整单元测试：150 项通过。
- Ruff 静态检查通过。
- 构建工作流提供 Windows x64 与 Linux x64 单文件可执行程序。
