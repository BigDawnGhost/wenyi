# CLI 使用指南

[English](../usage.md) · [Web UI](../web.md) · [配置指南](configuration.md)

## 安装与配置

```bash
uv sync --package wenyi-cli
export DEEPSEEK_API_KEY=your-key
uv run trans-novel --help
uv run trans-novel languages
```

首次启动时，若指定的配置文件不存在，CLI 会创建默认配置。自定义文件放在命令前：`uv run trans-novel --config custom.yaml translate book.epub`。凭证通过提供商配置中的 `api_key_env` 指定环境变量，不把密钥写入项目 YAML。

源语言 `language.source` 可设 `auto`；目标语言必须是内置的具体语言。相同源语言与目标语言会被拒绝。明确指定源语言可省略检测调用。

## 书籍流程

```bash
# 仅准备：解析、语言、风格、初始术语与可选全书梗概。
uv run trans-novel prepare book.epub
# 翻译待处理正文，润色、审校、发布修订、生成报告并导出。
uv run trans-novel translate book.epub
# 同时生成单语版与双语版。
uv run trans-novel translate book.epub --bilingual
# 只翻译第 0 章（章节索引从 0 开始）。
uv run trans-novel translate book.epub --chapter 0
# 本次关闭可选阶段。
uv run trans-novel translate book.epub --no-polish --no-review
```

默认开启全书预理解、润色、Review 和 Autofix。可在 YAML 关闭 `pipeline.book_understanding`；Web“快速出稿”会一起关闭预理解、润色、审校与自动修复。

书籍输入支持 EPUB、DOCX、FB2、TXT、Markdown、HTML 和 PDF。`prepare` 虽不翻译正文，仍可能为语言检测、风格分析和梗概调用模型。Web 上传预览是独立解析任务，不调用翻译模型。

### 中断续跑

停止后，用相同源文件、目标语言和配置重复执行命令。系统保存已完成的翻译批次、术语提取检查点、章节与上下文；缺失术语检查点时补提取，不重新翻译已有正文。润色保留润色前译文和最终译文。

CLI 状态位于 `state/<书名>/targets/<语言>/`。续跑会校验源文件 SHA-256；源内容改变需使用新的运行目录。不同目标语言使用独立目录。Web 项目初始化后固定源文件身份与目标语言，改变其中任意一项需新建项目。

## 全书审校与自动修复

```bash
# 使用 pipeline.review_autofix 的值，默认 true。
uv run trans-novel review book.epub
# 仅生成建议，不发布到正式译文。
uv run trans-novel review book.epub --no-autofix
# 本次明确开启发布。
uv run trans-novel review book.epub --autofix
```

全书审校要求所有章节均已完成翻译。Review 基于快照和影子译文核查证据、仲裁冲突、临时修订并盲审；独立 Autofix 阶段通过前后文本哈希与发布索引写回正式译文。

**不是每次执行都会新建审校运行。** 译文内容、审校配置/模型路由、术语指纹一致时，已完成结果会被复用，未完成的可恢复运行会继续检查点；相关输入变化才建立新运行。未完成的 Autofix 发布先恢复，再考虑新审校；已被人工修改的正文不会被过期修复覆盖。

`--no-autofix` 控制正式发布，不关闭影子修订循环；仍可计算建议变更。运行记录包含问题、建议、实际发布记录、失败原因、证据、检查点与用量。查看问题数量时应同时查看完成状态；未审校不表示“零问题通过”。

## 状态与术语

```bash
uv run trans-novel status book.epub
uv run trans-novel report book.epub
uv run trans-novel glossary list book.epub
uv run trans-novel glossary conflicts book.epub
uv run trans-novel glossary resolve --help
```

术语提取保留既有映射，将不同候选译法记录为冲突，由用户明确解决。存储按插入顺序提供术语，保持提示词前缀稳定。用量跨运行累计；计时记录区分完成、中断与失败。

## 导出与 PDF

```bash
uv run trans-novel assemble book.epub
uv run trans-novel assemble book.epub --format html
uv run trans-novel assemble book.epub --format docx
uv run trans-novel assemble book.epub --format pdf --pdf-engine weasyprint
uv run trans-novel assemble book.epub --format pdf --pdf-engine fpdf2
uv run trans-novel translate manuscript.docx
```

支持 `epub`、`txt`、`html`、`markdown`、`docx`、`pdf`。CLI 的 DOCX 输入默认导出 DOCX；通过 BabelDOC 解析的 PDF 默认导出 PDF；MinerU 与普通书籍默认导出 EPUB，除非显式选择格式。文件名含目标语言，双语版加 `-bi`。

独立 `assemble` 在短时状态锁内读取一致快照，释放锁后排版，可在翻译继续时导出已保存内容。标点规范化只改变导出副本。EPUB/HTML 保留支持的资源、目录和注释；DOCX 保留支持的样式、列表和表格。复杂书籍仍需检查实际排版。

MinerU 需要 `MINERU_API_KEY`，解析结果按源文件与配置缓存。选用 BabelDOC 时设 `pipeline.pdf_backend: babeldoc` 和可访问的 `pipeline.babeldoc_bridge_url`，独立部署 bridge。PDF 输出需对应 Python extra 与系统字体/库，容器默认值见 [Web 部署](../web.md)。

## SRT 字幕

```bash
uv run trans-novel translate captions.srt
uv run trans-novel translate captions.srt --bilingual
```

SRT 采用重叠窗口并发翻译，在 `state/srt/<名称>/targets/<语言>/` 保存字幕条目和批次缓存，保留序号与时间戳，输出单语和/或双语 SRT。重复执行恢复未完成工作；已保存字幕和 Web 人工编辑优先于旧批次响应。字幕不运行书籍预理解、术语提取和全书审校。

## 模型检查与对比

```bash
uv run trans-novel models list
uv run trans-novel models explain --operation review.scan
uv run trans-novel models check --workflow translate
uv run trans-novel models compare --operation translation.body \
  --model default_strong --messages messages.json --out comparison.json
```

`messages.json` 是由 `role`/`content` 对象组成的 JSON 数组。重复 `--model` 可选择多个模型配置。对比会明确向这些模型发送测试消息，并记录输出、耗时与用量；查看路由不发送对比请求。

当前流程已移除独立 `qa` 命令和回译抽检。旧 `do_qa`、`autofix_severe`、`review_agent_tier`、`force` 和字符预算字段不属于当前 API/配置；改用 Review Autofix、操作路由与 Token 预算。
