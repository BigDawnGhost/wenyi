# 创建项目布局预览 / Project creation layout previews

这些独立演示用于比较排版；**正式创建弹窗已采用 A 的紧凑布局**。每份 HTML 都内联自己的 CSS 和
JavaScript，可以单独复制或直接用浏览器打开，不依赖 React、API 或外部资源。
演示中重复的样式和交互是有意的：正式实现复用现有组件，没有引入这些演示副本。

These standalone prototypes remain available for comparison; **the production dialog now uses layout A**.
Each HTML file contains its own CSS and JavaScript and opens independently, without
React, a backend or external resources. Duplication is intentional for standalone
previews; the production implementation reuses existing components, not these copies.

| 方案 / Option | 文件 / File | 侧重点 / Focus |
| --- | --- | --- |
| A · 紧凑单栏 / Compact | `a-compact.html` | 合并语言和风格行，减少纵向留白 / Combine language and style fields to reduce height |
| B · 左右双栏 / Split | `b-split.html` | 原文和目录在左，翻译设置在右 / Source and location on the left, translation settings on the right |
| C · 原文优先 / Source first | `c-source-first.html` | 先选原文，再按对齐的设置行配置 / Start with the source, then use aligned settings rows |

打开 `index.html` 对比方案，或从仓库根目录启动本地预览服务：

Open `index.html` to compare the options, or serve them locally from the repository root:

```bash
uv run --no-sync python -m http.server 4189 --bind 127.0.0.1 \
  --directory packages/ui/sandbox/project-create-layouts
```

浏览器访问 / Open: http://127.0.0.1:4189/

可以选择或拖入临时文件、切换风格和任务开关。文件内容不会被读取，不会上传、
创建项目、访问目录或保存设置；目录按钮仅提示正式应用的行为。

Use temporary files to try selection, drag-and-drop, styles and task switches.
The prototypes do not read file contents, upload files, create projects, access
directories or persist settings. The directory button only explains the production action.

已安装工作区前端依赖和 Playwright Chromium 后，从仓库根目录验证：

With workspace frontend dependencies and Playwright Chromium installed, validate from the root:

```bash
pnpm -C packages/ui exec node sandbox/project-create-layouts/check.mjs
```

检查包括独立文件打开、离线交互、默认值、文件校验、PDF 选项及短窗口布局。
设置 `DELTA_SCRATCH_DIR` 时还会将三张预览图写到该目录。

The checks cover direct-file loading, offline interactions, defaults, file validation,
PDF options and short windows. When `DELTA_SCRATCH_DIR` is set, three preview images
are also written to that directory.
