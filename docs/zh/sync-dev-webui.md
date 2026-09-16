# `webui` 同步 `dev` 的实现说明

[English](../sync-dev-webui.md)

## 基线与原则

本次移植以 `webui@ff65cddf600c1c0cbdc6023625a820361644ad9b` 的架构为基础，对齐 `dev@851159271e3f255ebafc0d67e26818c45209a5c8` 的源码和测试。React/Vite、FastAPI、Arq、Redis、PostgreSQL 及 core/CLI 分包继续保留；领域能力按模块移植，未整分支合并。

部署使用全新数据库结构，不提供旧 Web 数据、策略或项目的自动迁移。源文件或目标语言更换后建立新项目。

## 功能对应

| 能力 | 实现位置 / 验证方向 |
|---|---|
| 多语言、元数据和提示词 | core `i18n`；语言检测、变体、同语言拒绝、54 份资源打包检查。 |
| 提供商与模型路由 | core `llm`；连接/模型/档位/操作路由、重试、预算、备用路由、用量测试。 |
| 翻译、润色、上下文与术语 | core 领域服务；Token 组批、同会话润色、预润色文本、术语检查点、标题与恢复测试。 |
| EPUB / HTML / DOCX / PDF | 独立 ingest/assemble 模块；样式、表格、注释、Ruby、资源/导航、PDF 后端/默认导出测试。 |
| Review / Autofix | core `review` 与领域服务；完成复用、中断恢复、指纹变化、影子修订、发布幂等、人工修改保护。 |
| SRT | core `srt`；时间轴、批次缓存、暂停用量、恢复不重复计费、人工字幕编辑保留。 |
| Web PostgreSQL | 存储接口注入；事务回滚、项目锁、MVCC 快照、完整工作流和无本地状态副本测试。 |
| Web 执行与导出 | 工作流/导出分队列；异步解析、模型配置、Review 历史、字幕编辑、人工校阅、导出下载；持久任务身份与独立恢复循环处理异常退出。 |
| CLI 分包 | `packages/cli/wenyi_cli`；`trans-novel` 和 `python -m wenyi_cli`，核心不依赖终端模块。 |

已删除独立一致性 QA、回译抽检、旧严重问题修复选项和旧模型档位/字符预算字段。标点规范化位于输出配置，仅操作导出副本。

## Review 文档纠正

旧使用文档将 Review 描述成“每次全量重审并创建新目录”“默认只读”。当前源码行为是：

- 内容、审校配置/路由和术语指纹一致时，复用已完成结果或恢复同一次中断审校。
- `pipeline.review_autofix` 默认 `true`；仅看建议可设 `false` 或使用 `--no-autofix`。
- Review 内部影子修订仍独立于正式发布，Autofix 使用可恢复发布索引和文本哈希保护人工修改。

中英文 README、使用、配置和流水线说明均已按这些行为更新。

## 验证记录与边界

最终全量测试：**Python 3.12：878 passed、1 skipped；Python 3.10：875 passed、4 skipped**，两者均另有 49 subtests passed。Ruff 与 `git diff --check` 通过；core/CLI/API 的 sdist 和 wheel 构建通过，并核对核心 wheel 包含全部 **54** 份语言、提示词和导出资源。新增资源目录的 Git 忽略例外，避免 `data/` 与 `*.txt` 规则把发布资源排除。

真实 PostgreSQL 集成测试覆盖共享存储契约、回滚、锁、快照和完整书籍/字幕流程；相关测试位于 `apps/api/tests/test_storage_pg_integration.py`，需 `WENYI_TEST_DATABASE_URL`。前端类型/构建/浏览器测试、API 任务测试由各自测试集及 CI 执行，最终运行结果以交付记录为准。

测试使用可控模型和 PDF 服务替身，不消耗真实模型凭证。这次未声称完成真实模型、长篇小说译文质量的前后对比，也未把离线通过等同于真实翻译质量结论。Docker 镜像和外部 MinerU/BabelDOC 服务需要在具备对应运行条件的环境进行部署验收。

完整的实际验收范围、并发边界和外部验证限制见[验收记录](validation.md)。
