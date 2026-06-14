# trans-novel —— 多 Agent 协同长篇小说翻译（日/英 → 中）

一套以"媲美人类翻译、尽量减少漏译/出错、靠专有名词对照表保证一致性"为目标的流水线。
多个职责单一的 Agent 协同，模拟出版社流程：**分析 → 翻译 → 审校 →（重译严重项）→ 润色 → 标点规范化 → 术语 AI 审计统一 → 跨章一致性把关**，全程围绕一个持久化的术语库。**一条 `translate` 命令连续跑完并直接产出 EPUB。**

- **语言方向**：日/英 → 中，**自动检测来源语言**（`config.yaml` 设 `language.source: auto`，也可写死 `ja`/`en`）。提示词按源语言切换（日语：敬称/假名读音/第一人称语气；英语：性别推断/从句重组/称谓处理）。
- **模型**：DeepSeek 双档，经 OpenAI SDK 调 `https://api.deepseek.com`，两档都开 thinking 模式、`reasoning_effort=high`（成本差异只靠模型区分）：
  - `strong` = `deepseek-v4-pro` → 翻译 / 润色 / 全局分析 / 术语审计
  - `cheap`  = `deepseek-v4-flash` → 术语抽取 / 审校 / 一致性 / 回译
- **输入**：EPUB 与 纯文本（TXT/Markdown）。**输出默认 EPUB**（EPUB 输入按原模板回填保留排版；TXT 输入用 ebooklib 生成规范 EPUB3）；`--format txt` 可导出纯文本。
- **标点**：译文统一为简体中文大陆通用全角标点（“”‘’、，。！？、……、——）。

## 安装

```bash
uv sync                          # 用 uv 安装依赖（pydantic / typer / rich / tenacity / ebooklib / lxml / openai …）
export DEEPSEEK_API_KEY=sk-...   # DeepSeek API key（运行真实翻译时需要）
```

> 仅离线跑切分/对齐/术语库/状态机等逻辑（不发网络请求）时，把 `config.yaml` 的
> `llm.provider` 设为 `fake` 即可。

## 使用

```bash
uv run trans-novel translate book.epub   # 连续全流程：分析→翻译→审校→润色→标点→术语审计→QA→出 EPUB（断点可续）
uv run trans-novel resume    book.epub   # 中断后续跑，跳过已完成章节
uv run trans-novel status    book.epub   # 查看各章进度与术语库统计
uv run trans-novel glossary  book.epub list      # 查看术语表
uv run trans-novel glossary  book.epub conflicts # 查看待裁决的译法冲突
uv run trans-novel glossary  book.epub audit     # AI 审计统一译法并改写正文
uv run trans-novel qa        book.epub   # 全书跨章一致性扫描
uv run trans-novel report    book.epub   # 生成 QA 报告（漏译/冲突/低置信度汇总）
uv run trans-novel assemble  book.epub   # 回填生成译文 EPUB（--format txt 出纯文本）
```

`translate` 自带段级进度条（不止于章），长文也能看清进度。
开关：`--no-polish` / `--no-audit` / `--no-qa`；调试单章：`translate book.epub --chapter 0`。

### 可视化 Web 前端（可选）

```bash
uv run trans-novel web book.epub          # 启动后打开 http://127.0.0.1:8000
```

浏览器里可：勾选要跑的步骤（翻译/术语审计/QA/报告/回填，可单选可全选）→ 运行；
**实时**看进度条与"原句↔译句"批次级对照（最新批次动态出现在最上方）；查看**建议/修订**（审校建议、回译疑点、一致性问题、术语统一，并标注 ✓已修订 / ⚠仅建议）；
**直接在前端编辑专有名词表**：改译法（保存即把旧译法改写进正文）、锁定、删除、裁决冲突、单条"应用到正文"，以及"重新应用术语表到正文"一键统一全书。核心 CLI 不受影响，Web 为纯附加。

> 自动改写边界：**术语审计**会自动改写正文（消除译法漂移）；**一致性 QA / 报告**只汇总不改正文——需要的改动通过上面的术语表编辑/重新应用来落地。

### 连续流程（默认）

`translate` 一步到位：先用强档分析样章建立术语表 → 章内批次并发翻译（章首上下文快照、跨章串行保连贯）→ 审校重译 → 润色 → 标点规范化 → 术语 AI 审计统一（消除如 佳穂/佳穗 的译法漂移并改写正文）→ 跨章一致性 QA → 写报告 → 回填出 EPUB。

## 一致性 / 防漏译机制

- **句段对齐强制**：翻译按批输入 N 段、要求输出 N 段 JSON 数组；段数不符则重试，
  仍不符则逐段兜底翻译，从结构上杜绝整段漏译。
- **专有名词对照表（SQLite）**：人名/地名/术语/敬称统一译法，含读音、性别、别名、
  置信度、锁定位；每章增量抽取、冲突裁决，翻译时只注入相关条目。
- **滚动上下文**：故事梗概 + 前文译文尾段，保证跨批次/跨章连贯与代词指代。
- **多遍校验**：廉价档审校（漏译/误译/术语/人称）→ 严重项回炉重译；可选回译抽检；
  强档润色（先准确后流畅）；全书跨章一致性扫描。
- **断点续跑**：以章为最小单元，状态原子落盘，`resume` 跳过已完成章节。

## 配置（`config.yaml`）

模型 ID、双档 effort、流水线开关（审校/润色/回译比例/一致性）、敬称策略、切分粒度都在这里改。

> `deepseek-v4-flash` 
> `deepseek-v4-pro` 

## 目录

```
trans_novel/
  ingest/      摄取与切分（EPUB/TXT → Chapter/Segment）
  llm/         LLM 抽象接口 + DeepSeek provider + 离线 FakeClient
  glossary/    术语库(SQLite) + 抽取 + 冲突裁决
  agents/      analyzer / translator / reviewer / polisher / consistency + 提示词
  pipeline/    orchestrator(状态机) / context(滚动上下文) / checks(对齐校验) / runstore
  assemble/    回填(EPUB/TXT) + QA 报告
prompts/       提示词覆盖（可选，见其 README）
tests/         离线测试（不发网络请求）
```

## 实现取舍

- LLM 层是**可插拔接口**：要换平台只需实现 `LLMClient`（见 `llm/base.py`），其余不动。
- 建模用 `pydantic`，LLM 重试用 `tenacity`，CLI/进度用 `typer`+`rich`，TXT→EPUB 用 `ebooklib`。
- EPUB 输入回填仍走 zip 原样拷贝 + 锚点替换，最大程度保留原排版/资源。
- 章内批次并发提速，章首对滚动上下文取快照以保证可并发与确定性；跨章串行传递故事梗概保连贯。

## 测试

```bash
uv run python -m unittest discover -s tests
```
