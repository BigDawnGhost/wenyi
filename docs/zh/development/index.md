# 开发与维护文译

本区面向贡献者与维护者。安装发行版或翻译书籍请阅读使用指南，不需要搭建源码开发环境。

## 找到负责的层

| 任务 | 文档 |
| --- | --- |
| 理解包、依赖和存储端口 | [架构与模块边界](architecture.md) |
| 修改检查点、恢复、语言策略或提示词 | [流程开发契约](pipeline.md) |
| 启动或修改 API、worker 和 Web 宿主 | [Web 开发](web.md) |
| 启动或修改原生应用与本地引擎 | [Desktop 开发](desktop.md) |
| 修改共享 UI、路由、加载和交互约定 | [前端规范](frontend.md) |
| 构建与发布桌面安装包及更新 | [桌面发布](releasing.md) |
| 将 CLI 和 Core 发布到 PyPI | [Python 包发布](publishing.md) |

从源码开发请先阅读[贡献指南](../CONTRIBUTING.md)，其中维护分支与审查规则、
CLI 源码启动和 Python 回归测试命令。
[仓库指南](../../../AGENTS.md)记录编码 Agent 的安全约束和任务入口。
本地验证使用假客户端和临时数据，不对生产项目或服务运行测试。

## 文档结构

```text
docs/
  index.md          # 文档站首页
  guide/            # 安装、使用与部署
  reference/        # 配置与格式查询
  development/      # 架构、开发、测试与发布
  zh/               # 简体中文，镜像相同主题
  images/           # 共享文档资源
  .vitepress/       # 站点配置
```

文档描述当前行为，并归入负责的主题。在现有章节中修改、合并、删除过时说明，
不要追加 UI 移动记录或修复流水账。Desktop/Web 的共有操作集中在 `guide/workspace.md`，
不维护两套重复手册。中英文同步，其他页面链接到主要说明，而不是复制正文。
完成的设计讨论和测试运行记录保留在 Git、PR 或发行历史中，只有长期有效的决策进入对应
开发文档。完整维护规则见 [AGENTS.md](../../../AGENTS.md#文档维护)。

## 本地预览文档站

安装 Node 22 和 pnpm 9 后，在仓库根目录执行：

```bash
pnpm install --frozen-lockfile
pnpm docs:dev
```

英文首页为 `http://127.0.0.1:5175/wenyi/`，中文首页为
`http://127.0.0.1:5175/wenyi/zh/`。验证静态站点：

```bash
pnpm docs:build
pnpm docs:preview
```

静态预览使用端口 `4175`。两个服务都只监听 `127.0.0.1`；重新构建后需重启静态预览。
这些命令不会发布站点。仓库专用文件和源码链接在 GitHub 打开；
Mermaid 图仍显示为代码块。

移动页面后，除了 Markdown 链接，还要更新导航、仓库入口、包文档链接和源码注释。
检查章节锚点、双语页面、搜索结果，并运行 `git diff --check`；
构建成功不等于所有链接都能抵达正确章节。

## GitHub Pages 部署

文档站发布地址为
[bigdawnghost.github.io/wenyi](https://bigdawnghost.github.io/wenyi/)，
中英文通过站点语言菜单切换。这个项目站点地址要求 VitePress 的 `base` 保持 `/wenyi/`。

[Documentation 工作流](../../../.github/workflows/docs.yml)使用 Node 22、
pnpm 9、冻结锁文件和 `pnpm docs:build` 构建。PR 只验证，不发布；
相关变更合入 `main` 后自动构建部署。维护者也可在
**Actions → Documentation → Run workflow** 中选择 `main` 手动触发。

在仓库 **Settings → Pages** 中，将 **Build and deployment → Source** 设为
**GitHub Actions**。部署使用 `github-pages` 环境，应只允许 `main`，
并保留已有审批要求。只有部署 job 获得 `pages: write` 与 `id-token: write` 权限，
无需个人 token 或额外密钥；新 push 不会取消已进行的正式部署。

只上传 `docs/.vitepress/dist/`，不能上传仓库根目录、本地预览缓存、`.env` 或应用工作区。
发布的是静态文档，不是文译 Web 应用或 API。部署后在公开地址检查双语首页、
深层文档链接、资源、搜索和语言菜单。失败时检查工作流与 Pages 设置，
不要提交构建产物或绕过部署保护。
