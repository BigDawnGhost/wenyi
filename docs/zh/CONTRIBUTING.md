# 贡献指南

[English](../../CONTRIBUTING.md) | **简体中文**

感谢你愿意让这个项目变得更好。本项目优先关注长篇小说翻译质量。

## 可以贡献什么

欢迎提交：

- 输入解析：EPUB、FB2、TXT、DOCX、SRT 等格式兼容性扩展与改进。
- 翻译流程：上下文、术语表、审校、润色、一致性检查。字幕相关改动放在独立的 `wenyi_core.srt` 流程中。
- 导出：EPUB/DOCX 输出、目录、元数据、排版保留，以及 SRT 写出。
- 测试：真实失败样例、回归测试、离线 fake LLM 测试。
- 文档：使用说明、配置解释、常见问题。

注意，如果涉及核心翻译流程，即对翻译质量可能有影响的，请先测试一本不少于五万字的公版小说，提供修改前后版本对比分析，证明确实可以改进翻译质量。

代码注释、docstring、配置注释、CLI 文案和提示词指令统一使用标准英语。语言规则和任务模板集中在 `packages/core/wenyi_core/i18n/data/`。模型生成的说明性内容（包括术语备注和分析描述）使用翻译目标语言；原文 `source` 和 `aliases` 保持不变，用于匹配。保留语言专属示例和多语言测试数据，并同步维护英文和中文文档。

## Python 回归测试

按规则所属层组织测试：Core 覆盖领域规则和持久化、续跑不变量；共享 backend 覆盖平台无关的应用及 HTTP 契约；API/Desktop 覆盖适配装配、平台存储、队列和原生文件安全。Orchestrator 测试保留跨服务流程，不重复下层服务的细粒度矩阵。fixture 通过 `conftest.py` 共享，辅助函数放在 support 模块中，不通过导入其他 `test_*` 模块复用。

从仓库根目录执行：

```bash
uv sync --locked --all-packages --group dev
uv run --no-sync python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
uv run --no-sync pytest -q -m "not integration"  # No external test services.
uv run --no-sync pytest -q -m integration        # Isolated service tests.
uv run --no-sync pytest -q                      # Full collection, as before.
```

词表准备命令在缓存缺失时下载 `cl100k_base`；离线测试前先联网准备。测试保留真实 tokenizer，不用 fake 替换 token 预算语义。CI 在回归步骤之前准备词表，避免首次下载占用本地任务测试的超时预算。

依赖 `pg_pool` fixture 的 API 测试（包括间接依赖）会自动获得 `integration` 标记。动态请求 PostgreSQL fixture 的参数需要显式标记该参数，文件存储用例仍属于离线测试。SQLite、子进程、崩溃恢复和本地工作流测试保留在离线测试集中。

`WENYI_TEST_DATABASE_URL` 和 `WENYI_TEST_REDIS_URL` 只能指向隔离测试服务。PostgreSQL fixture 会创建私有 schema 并在结束时删除。Docker 代理测试也标记为 `integration`，还需设置 `WENYI_TEST_DOCKER=1`。缺少服务导致的跳过不算集成验证通过。CI 在 Python 3.10 和 3.12 下分别运行两组测试并启用 PostgreSQL、Redis；Desktop 打包保留各平台测试。
