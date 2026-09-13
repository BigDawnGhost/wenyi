# 主代码维护性重构设计

[English](../../refactoring/README.md)

状态：实施中，各方案单独记录已完成切片。以下清单审查于 2026-09-11–12，基线为本地
`dev` 的 `7471256`，已包含总体计时功能。实施从 `eef85d4` 开始，保留后续 Review 可恢复错误修复。

## 结论

值得重构，架构优先级最高的是 Review 工作流和 EPUB 共享标记处理边界。实际落地建议先做较小的 CLI 展示层和标题翻译拆分，建立验证方式，再处理状态复杂的模块。保留现有薄 Orchestrator、provider 注册和 operation 注册；不能仅因为文件长就拆。

本次检查了产品 Python 源码、导入关系、关键方法和相关测试。行数包含注释与空行，函数长度包含内部函数，仅用于定位，不代表圈复杂度或性能结论。未使用私有书籍、状态、输出或外部服务。

## 长文件清单与判断

| 当前文件 | 行数 | 最长函数 | 判断 |
| --- | ---: | --- | --- |
| `pipeline/review_workflow.py` | 1,891 | `run_session`，717 行 | 高优先级：会话状态、恢复、执行与结果组装交叉。[01](01-review-workflow.md) |
| `ingest/epub_reader.py` | 1,561 | `_logical_chapters`，206 行 | 高优先级：容器读取与可复用标记处理混在一起。[04](04-epub-markup.md) |
| `agents/review_loop.py` | 1,006 | `_ActionLoop.run`，300 行 | 高优先级：对话重放、协议校验与仲裁需要明确分工。[02](02-review-agents.md) |
| `cli.py` | 953 | `_translate_impl_or_raise`，110 行 | 中优先级，适合作为首个小改动：拆展示、校验、命令入口。[06](06-cli.md) |
| `pipeline/review_autofix.py` | 922 | `_run`，604 行 | 高优先级：候选规划与索引发布分离。[03](03-review-autofix.md) |
| `assemble/epub_writer.py` | 858 | `_render_epub_resources`，139 行 | 与共享标记处理一起重构。[04](04-epub-markup.md) |
| `pipeline/translation.py` | 791 | `translate_titles`，258 行 | 中优先级：先拆标题，再显式表达批次提交顺序。[05](05-translation.md) |
| `assemble/html_renderer.py` | 747 | `_render_segments_html`，120 行 | 在 EPUB 工作中拆行内恢复与双语链接。[04](04-epub-markup.md) |
| `assemble/docx_writer.py` | 737 | `_emit_chapter_blocks`，131 行 | 中优先级：区分样式策略、Word 写入与块级组装。[07](07-docx.md) |
| `pipeline/runstore.py` | 607 | `begin_initialization`，49 行 | 暂不整体拆：多数存储方法短且内聚，事务边界敏感。 |
| `agents/annotation_aligner.py` | 526 | `_align_batch`，107 行 | 暂缓：复杂度主要属于一个对齐协议；出现第二种对齐策略再评估。 |
| `review/run_store.py` | 516 | `find_resumable`，56 行 | 保留持久化职责；按 02 移出纯类型。 |
| `ingest/docx_reader.py` | 515 | `read_docx`，144 行 | 仅按 07 提取共享策略，不重写 reader。 |
| `glossary/store.py` | 433 | `upsert_term`，61 行 | 保持 SQLite 术语及冲突操作集中。 |
| `pipeline/orchestrator.py` | 404 | `_finish_steps_locked`，82 行 | 保留薄门面，不扩展成工作流或插件框架。 |
| `srt/translate.py` | 385 | `translate_srt`，206 行 | 观察项：下次修改字幕时考虑窗口规划、稳定合并和持久化边界，不并入书籍流程。 |

`config.py` 为 221 行、`model_commands.py` 为 217 行，LLM 模块也不是当前长文件的主要问题。`register_model_commands` 的长度主要来自显式命令声明；沿用其注册方式即可，不需要动态扫描命令插件。

## 所有拆分共用的约束

- 分离纯决策、I/O 和可变状态所有权。把方法搬进多个 mixin、继续共享整个 `self._runtime`，并没有完成解耦。
- 保持 `CLI → Orchestrator → 领域服务 → agents / ingest / assemble / stores`。新模块不得反向导入门面；agents 只依赖纯 Review 类型与契约，不依赖具体存储实现。
- 提取阶段保留 operation ID、提示词字节、请求顺序、模型选择、缓存指纹、段落和锚点身份、磁盘格式及提交顺序。仓内调用和测试一次更新，不为旧私有 API 保留别名或兼容包装。
- 后续若要改磁盘格式、提示词或质量策略，另开功能变更，按需要显式迁移与评估。保持当前状态正确性，不等于增加历史兼容分支。
- 保留各专用锁、manifest 最后提交、导出快照、用量增量合并和最外层工作流计时。不能在每个新服务上再套计时，造成累计重复。
- BabelDOC 继续通过 HTTP 隔离；不引入 AGPL Python 依赖或新的联网要求。

## 建议实施顺序

A 批次已完成：进度条、摘要展示各自成模块；标题规划、模型调用、manifest 提交已有明确职责。
进度条、摘要、标题规划、标题 agent/service 分别独立提交。
B 批次也已完成：纯 Review 类型与冲突规则、trace/证据接口、对话回放和仲裁器各自成模块。
内存续跑测试覆盖 9 个对话中断边界，agents 不再导入具体 Review 存储。
C–F 及后续 CLI 上下文、正文批次切片仍待实施。

| 批次 | 内容 | 前置条件与验收门槛 |
| --- | --- | --- |
| A | 06 的展示层；05 的标题规划与翻译 | 两项独立的小改动，CLI 行为与标题检查点不变。 |
| B | 02 的纯 Review 契约和对话重放 | 先固定现有 trace 恢复语义，再动全书协调器。 |
| C | 01 的检查点转换、决策、审校块执行、协调器 | 基于 B，每次移动都验证失败与续跑轨迹。 |
| D | 03 的候选规划与索引发布器 | 复用已明确的 Review 类型，发布仍是独立服务。 |
| E | 04 的共享标记处理，再拆 EPUB 读写内部 | 可独立于 B–D；避免与标题身份规则变更同时修改。 |
| F | 07 的 DOCX 样式策略和写入层 | 共享策略归属确定后可独立进行。 |

每批可以拆成多个小 PR。不要在一个 PR 同时移动目录、改磁盘格式和改行为。优先级表示维护风险，并不是认定当前输出有错；Review 和 EPUB 的主要成本在回归验证，不承诺未经测量的提速。

评审时关注：协调器能否直接读出阶段顺序；模块是否只有一个状态所有者和一个主要修改原因；新增功能是否还要导入某个工作流的私有函数。每个内聚实现模块约 200–500 行、协调函数尽量低于 100 行可作提醒，不设硬性行数 CI 门槛。

## 验证与完成标准

当前基线 `uv run --no-sync pytest -q` 通过 **714 项测试、49 项子测试**。这只确认现有离线基线，不代表未来重构已证明等价，也不是翻译质量评测。

每次实施前保留或补充对应公开边界的行为测试。比较 FakeClient 的 operation 与消息序列、结构化产物、稳定 ID、检查点内容、用量总额和语义事件顺序；排除时间戳、临时路径和本次运行 ID。有顺序契约的请求必须保持顺序；并发请求按任务键比较，并验证合并结果稳定。EPUB/DOCX 比较内部结构，不直接要求 ZIP 字节相同。

运行受影响测试、`test_architecture_boundaries.py`、`test_orchestrator_contract.py`、Ruff 和 `git diff --check`；跨模块状态修改运行全量测试。新建包后扩展架构测试为递归检查，并禁止被移除的依赖关系；当前测试仍包含显式文件、模块名单。

提取测试不调用真实模型、不读取私有样例。如果实际改变提示词、上下文、终止策略或其他翻译行为，另按 `CONTRIBUTING.md` 进行公版长文前后评估。

[2026-09-05 的 Review 方案](../project-review/2026-09-05/p04-review-state-machine-refactor.md) 保留为历史背景。01–03 基于当前源码细化该方向，不把旧问题编号直接当成仍未修复的前置事项。
