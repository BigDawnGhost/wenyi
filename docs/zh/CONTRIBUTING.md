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

## 围绕 main 的主干开发

`main` 是唯一长期集成分支，必须保持可构建、通过测试、可发布。使用小而短期的
`feat/*`、`fix/*`、`docs/*` 或 `chore/*` 分支，PR 直接指向 `main`，不再经过
`dev`、`webui` 或独立发布集成分支。将大改动拆成可以独立安全合入的 PR，未完成的
行为不得进入默认用户路径。

当 `origin` 指向 Wenyi 仓库时，从最新主干开始新工作：

```bash
git fetch origin
git switch -c feat/short-description origin/main
# Implement, verify, and commit only the intended changes.
git push --set-upstream origin feat/short-description
gh pr create --base main --head feat/short-description
```

合并前将最新 `origin/main` 合入任务分支，解决冲突但不改写已发布历史。检查相对
`main` 的完整差异，而不只看最后一次提交。最终候选及当前基线必须通过完整
`Tests` 工作流（Python 3.10、Python 3.12 和 web），以及适用的 CLI/Desktop
打包检查。满足适用的审查要求，不绕过检查，不直接向 `main` 推送改动。默认继续
使用 merge commit 合入。仓库的 `/land` 流程以 `main` 为目标，仍须用户明确授权落地。

### 发布与修复

在 `main` 上经过验证的提交处创建版本标签，例如 `v1.2.8` 或 `v1.2.8rc1`，
然后为该标签发布 GitHub Release。发布 Release 会触发 CLI/Desktop 打包与产物上传。
仅合并 PR 或推送标签不会发布安装包；仍可手动触发工作流验证构建。修复同样经过
短期分支 → PR → `main`，再用新标签发布。不移动已有发布标签，不维护长期 hotfix 分支。

### 仓库设置与迁移

仓库管理员应保持 `main` 为默认分支，要求通过 PR 合入，启用 `Tests` 的
Python 3.10 / Python 3.12 / web 必需检查，要求分支基于最新主干，并禁止强推和删除主干。
CLI/Desktop 打包工作流有路径过滤，不应将这些条件检查设为所有 PR 的必需检查。
文档规则不会自动配置 GitHub 保护；需单独检查线上分支保护与 ruleset。

迁移已有工作时：

1. 检查仅在 `dev` 或其他旧集成分支上的改动，通过独立 PR 将需要保留的内容合入
   `main`。不要丢弃它们，也不要因为合并历史不同就认定全部改动都未进入 `main`。
2. 将剩余 PR 的目标改为 `main`，集成最新主干，重新检查完整差异，并要求针对新基线
   运行新的 CI。
3. 停止使用旧集成分支。只有确认需要的工作均已保留、且获得明确删除授权后才能删除。
   仅修改本规则不代表授权合并 PR、修改远端设置或删除分支。

## Python 回归测试

按规则所属层组织测试：Core 覆盖领域规则和持久化、续跑不变量；共享 backend 覆盖平台无关的应用及 HTTP 契约；API/Desktop 覆盖适配装配、平台存储、队列和原生文件安全。Orchestrator 测试保留跨服务流程，不重复下层服务的细粒度矩阵。fixture 通过 `conftest.py` 共享，辅助函数放在 support 模块中，不通过导入其他 `test_*` 模块复用。

从仓库根目录执行：

```bash
uv sync --locked --all-packages --group dev
uv run --no-sync python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
uv run --no-sync pytest -q -n auto -m "not integration"  # No external test services.
uv run --no-sync pytest -q -n 2 -m integration           # Isolated service tests.
uv run --no-sync pytest -q -n auto                       # Full collection.
```

按风险决定验证范围，而不是每次都跑全量。只改动单个领域时，跑该领域测试及其周边套件即可；一个批次收尾时跑一次全量，push 前再跑一次。请保留 `-n auto`：全量并行约 80-120 秒，串行需数分钟。`integration` 限两个 worker，因为它持有真实的 PostgreSQL 与 Redis 连接。

词表准备命令在缓存缺失时下载 `cl100k_base`；离线测试前先联网准备。测试保留真实 tokenizer，不用 fake 替换 token 预算语义。CI 在回归步骤之前准备词表，避免首次下载占用本地任务测试的超时预算。

依赖 `pg_pool` fixture 的 API 测试（包括间接依赖）会自动获得 `integration` 标记。动态请求 PostgreSQL fixture 的参数需要显式标记该参数，文件存储用例仍属于离线测试。SQLite、子进程、崩溃恢复和本地工作流测试保留在离线测试集中。

`WENYI_TEST_DATABASE_URL` 和 `WENYI_TEST_REDIS_URL` 只能指向隔离测试服务。PostgreSQL fixture 会创建私有 schema 并在结束时删除。Docker 代理测试也标记为 `integration`，还需设置 `WENYI_TEST_DOCKER=1`。缺少服务导致的跳过不算集成验证通过。

CI 在 Python 3.10 和 3.12 下分别运行两组测试并启用 PostgreSQL、Redis，并在本地沿用同样的分层：`changes` job 为每个 PR 分类，纯文档改动会跳过 Python 与 web job，而 push 到 `main` 与手动触发始终全跑。两个 Python 步骤都使用 `pytest-xdist`，integration 限两个 worker。Desktop 在 PR 上只跑 smoke 层，仅在 release 或手动触发时构建全部平台。integration 会重试一次，但只在重试后才通过的用例会写入 job 摘要并标为 flaky，因此重试不会掩盖真实问题。
