# trans-novel —— 多 Agent 协同长篇小说翻译（日译中）

一套以"媲美人类翻译、尽量减少漏译/出错、靠专有名词对照表保证一致性"为目标的流水线。
多个职责单一的 Agent 协同，模拟出版社流程：**分析 → 翻译 → 审校 →（重译严重项）→ 润色 → 跨章一致性把关**，全程围绕一个持久化的术语库。

- **语言方向**：日 → 中（提示词与术语字段专门设计：敬称、假名读音、人名性别、第一人称语气）。
- **模型**：DeepSeek 双档，经 OpenAI SDK 调 `https://api.deepseek.com`，两档都开 thinking 模式：
  - `strong` = `deepseek-v4-pro`（reasoning_effort=high）→ 翻译 / 润色 / 全局分析
  - `cheap`  = `deepseek-v4-flash`（reasoning_effort=low）→ 术语抽取 / 审校 / 一致性 / 回译
- **输入**：EPUB 与 纯文本（TXT/Markdown）。**输出默认 EPUB**（EPUB 输入按原模板回填保留排版；TXT 输入生成规范 EPUB）；`--format txt` 可导出纯文本。

## 安装

```bash
pip install openai beautifulsoup4 PyYAML   # 运行真实翻译需要 openai
export DEEPSEEK_API_KEY=sk-...             # DeepSeek API key
```

> 仅离线跑切分/对齐/术语库/状态机等逻辑（不发网络请求）时，把 `config.yaml` 的
> `llm.provider` 设为 `fake` 即可，无需 openai。

## 使用

```bash
trans-novel translate book.epub          # 新建任务并翻译（断点可续）
trans-novel resume    book.epub          # 中断后续跑，跳过已完成章节
trans-novel status    book.epub          # 查看各章进度与术语库统计
trans-novel glossary  book.epub list     # 查看术语表
trans-novel glossary  book.epub conflicts        # 查看待裁决的译法冲突
trans-novel glossary  book.epub lock    綾小路   # 锁定现有译法
trans-novel glossary  book.epub resolve 堀北 堀北 # 裁定并锁定某词译法
trans-novel qa        book.epub          # 全书跨章一致性扫描
trans-novel report    book.epub          # 生成 QA 报告（漏译/冲突/低置信度汇总）
trans-novel assemble  book.epub          # 回填生成译文 EPUB（--format txt 出纯文本）
```

调试：`translate book.epub --chapter 0` 只翻指定章。

### 推荐流程（质量优先时）

1. `translate`（会先用强档分析样章、建立初始术语表）。
2. `glossary ... review` 人工锁定核心人名、裁决冲突。
3. 继续 `resume` 翻完全书。
4. `qa` + `report`，按报告补查疑点。
5. `assemble` 出 EPUB。

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

> ⚠️ `deepseek-v4-flash` 为占位的廉价档模型名，请按 DeepSeek 实际可用的 flash 模型 ID 校正
> （`deepseek-v4-pro` 按你给的示例已填好）。

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
- 当前为零安装环境做了适配：建模用标准库 `dataclass`，EPUB 用 `zipfile`+`BeautifulSoup`
  解析（未强依赖 pydantic/ebooklib）。

## 测试

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
```
