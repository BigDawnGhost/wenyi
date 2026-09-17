# WebUI / `dev` 同步验收

[English](../validation.md)

## 验收范围

实现以 `webui@ff65cddf` 的 React/Vite、FastAPI、Arq/Redis、PostgreSQL 和 core/CLI 分包架构为基础，移植 `dev@85115927` 的领域行为。使用全新 Web 数据库结构；未进行旧项目迁移。

## 自动验证

最终验证结果：

| 检查 | 结果 |
|---|---|
| Python 3.12 全量（包含真实 PDF 字体测试） | 878 passed，1 skipped，49 subtests passed |
| Python 3.10 全量（基础依赖） | 875 passed，4 skipped，49 subtests passed |
| PostgreSQL / API / Worker 测试 | 82 passed，已包含在两版 Python 全量结果中 |
| Playwright 浏览器回归 | 7 passed |
| TypeScript / Vite 生产构建 | 通过 |
| Ruff / `git diff --check` | 通过 |
| core / CLI / API 的 sdist 与 wheel | 全部构建通过 |

Python 3.12 唯一跳过项是缺少仓库外的 Weaver PDF 样例；Python 3.10 另外跳过 3 项需要可选 fpdf2 的字体测试，这三项已在 Python 3.12 实际运行通过。两组 Python 输出仅有 FastAPI 测试客户端和 Arq 所调用 Redis 关闭接口的依赖弃用提示。

实际运行包含：

- Python 3.10 与 3.12：核心、CLI、真实 PostgreSQL 存储/API/Worker 测试。
- 真实 Redis：持久任务入队、Arq 消费及数据库状态回写。
- TypeScript、生产构建、7 项 Playwright 浏览器测试。
- core、CLI 和 API 分发包构建；核心包含全部 54 份语言、提示词和导出资源，API 包含数据库 schema 和后台恢复模块。
- 可选 PDF 渲染检查：验证字体嵌入、页面实际显示及文本，避免仅检查输出文件存在。
- Ruff 与 Git 空白差异检查。

## 真实浏览器流程

浏览器连接实际 FastAPI、PostgreSQL、Redis 和独立 Arq 工作流/导出 Worker，启用 Token 鉴权。仅模型客户端替换为可控的离线客户端，API 响应没有被模拟。

| 流程 | 验证结果 |
|---|---|
| EPUB | 创建、上传、异步解析、预览、项目配置校验保存、模型配置检查、准备、翻译、润色、默认 Review/Autofix、人工编辑均完成。 |
| 再次审校 | 人工编辑后建立新运行，原运行历史仍可查看。 |
| DOCX / EPUB 导出 | 浏览器下载成功；检查导出内容包含人工修订；持久 export job 的运行 ID 与队列 ID 一致且状态为 done。 |
| SRT | 上传、解析、翻译、时间轴显示、统计、人工编辑、双语导出完成；导出保留人工译文、原文及原时间戳。 |
| 鉴权 | HTTP 下载需要 Bearer token；WebSocket 在首包验证同一 token 后才发送项目数据。 |

以上浏览器流程未出现 JavaScript 异常或 API 500。最终页面截图：

- [全书审校](../images/web-review.png)
- [字幕对照与编辑](../images/web-subtitles.png)

## 状态和并发检查

- 文件存储与 PostgreSQL 共用领域契约；Web 不产生 JSON/SQLite 状态副本。
- 解析结果按原文件 SHA-256 与解析配置验证后复用。
- 已完成 Review 复用，中断 Review 和 Autofix 继续原检查点；人工修改不会被旧发布记录覆盖。
- 人工编辑后不再显示历史“已审校”状态及过期问题；新的审校完成后采用新结果。
- 模型请求等待期间没有进度回调，也能由暂停监视器调用取消并保存 paused 状态。
- 旧队列交付或迟到异常不会执行、覆盖新的任务。
- 队列提交失败、数据库写锁短暂竞争及 Worker 异常退出均有持久错误或恢复状态。
- 导出使用短事务一致快照，模型请求和渲染期间不持有数据库写事务。
- 导出有独立队列、运行身份、文件目录和锁；可与翻译并行，异常退出不会永久停留在 pending。
- 提交后的任务使用保存的配置快照；之后修改项目配置不改变已经提交的任务。

## 未执行的外部验证

- 未使用真实模型凭证进行长篇小说译文质量、成本或吞吐对比；离线通过不代表真实翻译质量结论。
- 未调用真实 MinerU 或部署 BabelDOC bridge；其解析、回填与失败路径由离线服务替身覆盖。
- 本机缺少 Docker Compose 插件且 Docker socket 不可访问，未实际构建和启动容器。Compose/Dockerfile 已更新并检查；CI 配置了 PostgreSQL/Redis、前端测试与可选 PDF 渲染依赖。
- Weaver 示例 PDF 未在仓库提供，对应集成样例测试按条件跳过。
