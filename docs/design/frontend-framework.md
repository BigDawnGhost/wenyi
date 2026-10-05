# 前端框架对比与优化计划

参考源码快照（仅本地分析用，不随本仓库分发）：

- Codex：`V:\codex`（openai/codex，Rust ratatui TUI，**无 Web UI**，对比维度为模式层）
- ZCode：`V:\zcode-src`（zai-org/ZCode，Apache-2.0，2026-09 快照；React 19 + Vite 8 + Electron，与本仓库技术栈同构，为主要对比对象）

## 1. 背景：一次真实事故的根因链

2026-10 的"点设置按钮页面刷新"bug（已修复，`3c42566`）暴露了四个结构性问题：

1. **Suspense 边界高于 shell**：`App.tsx` 唯一的 `<Suspense>` 包在 `<Routes>` 外，首次进入冷路由时，懒加载占位符把整个应用（含侧边栏）替换成全屏空白约 350ms——用户感知为"页面刷新"。
2. **嵌套 lazy 挂在紧急更新里**：Desktop 凭据组件 `DesktopCredential` 是平台层嵌套 `lazy`，它在 capabilities 查询返回（非 transition 的紧急更新）时挂起，占位符顶掉刚渲染好的整页内容——即"页内还是照样刷"。
3. **测试反向固化了错误行为**：E2E 曾断言路由挂起期间 `#root` 为空（`await expect(page.locator("#root")).toHaveText("")`），把"整页消失"钉成了规格。
4. **三处手工重复的路由信息**：`App.tsx` 的路由表、`routeLoaders` 预热数组、`Navigation.tsx` 的链接列表各自维护，无单一事实源，新增路由要改三处，容易漏（漏预热 = 闪屏回归）。

修复采用的手段（shell 内嵌 Suspense、启动预热、凭据字段局部边界）有效，但**只靠约定，没有护栏**——大改 UI 时极易被重新破坏。本计划的目标是把这次事故沉淀为框架规则与强制检查。

## 2. 三方对比

### 2.1 技术栈概览

| | Codex | ZCode | Wenyi |
|---|---|---|---|
| UI 载体 | Rust ratatui TUI | React 19 + Vite 8（`packages/ui` 共享 + `packages/web`/Electron renderer 薄宿主） | React 19 + Vite（`packages/ui` 共享 + `apps/web`/`apps/desktop/frontend` 薄宿主） |
| 路由 | 事件分发 + 视图栈（无 URL） | **无路由库**：Zustand tab store + view union + 历史栈；URL 仅用于分享页/OAuth | react-router 7 library 模式，13 条 URL 路由 |
| 代码分割 | 无（无 JS） | **仅组件级 lazy**（pdf/pptx/recharts 等重件 14 处），**无路由级分割** | 路由级 lazy（13 个页面 chunk）+ 组件级 lazy（3 处） |
| 导航保旧 | 导航时旧内容缓冲、新历史一次原子换页（`InitialHistoryReplayBuffer`） | **结构性保旧**：从不卸载 shell（设置页 overlay + `opacity-0/inert`、侧边栏 opacity 隐藏、Tabs `forceMount`），文档注释明确"整棵重建 = 闪烁" | 依赖 transition + 本次新加的 shell 内嵌边界；`startTransition` 实测在部分场景不保旧 |
| Suspense 位置 | — | **永不在 shell 之上**：入口 0 处 Suspense，全部是叶子边界（`ScopedErrorBoundary → Suspense → lazy`） | 修复后：shell 内 Outlet 一处 + 页面内 2 处 + 顶层兜底 1 处 |
| 错误边界 | panic hook + 终端恢复守卫，错误带内呈现不白屏 | **两级**：1 个全局 `AppErrorBoundary` + 14 处 `ScopedErrorBoundary`（21 个 scope，`resetKeys` 随 workspace/tab 切换自动复位，`panel/inline/compact/silent` 变体） | **1 个** `RouteBoundary` 在路由顶层：任一页面崩溃 = 整页错误页 + 整页 reload |
| 数据加载态 | 集中式 motion/加载指示（reduced-motion 强制降级） | 显式 `{snapshot, loading, error}` 三元组；**loading 只渲染在空槽位**，有数据时刷新绝不换 spinner；"门禁只拦首屏"+"旧数据优于空白"（单调 latch） | TanStack Query，页内 `isLoading`/`ErrorNotice` 散落各页，无统一约定 |
| 结构治理 | 模块 doc 即契约 | `architecture-policy.yaml` 可执行策略 + 基线检查器（`--changed` 增量、`disable-count` 惩罚抑制注释）、knip、TS project references、单一 `test-ids.ts` | `platform-boundaries.test.mjs`（平台边界 + lazy chunk 断言）+ Vite `generateBundle` 插件 |

### 2.2 ZCode 关键实践（与本仓库直接相关）

1. **结构性保旧，而非依赖 transition**。全仓库 0 处 `startTransition`。设置页打开时不替换主界面而是覆盖：

   ```tsx
   // RootWorkspaceContent.tsx — 设置页之前通过条件分支直接替换整个 App，
   // 关闭设置时会把主界面整棵树卸载再重建……用户看到的就是列表闪烁。
   // 这里改成让 workspace 壳层常驻挂载，只把设置页覆盖到上面。
   <div className={isSettingsTabActive ? "h-full opacity-0 pointer-events-none" : "h-full"}
        aria-hidden={isSettingsTabActive} inert={isSettingsTabActive || undefined}>
   ```

   切工作区也从"按 path 改 key 整棵重建"改为保留同一 App 实例、只清理强绑定状态（`useWorkspaceShellLifecycle.ts`）。**结论：shell 永不因导航卸载，是靠组件树结构保证的，不是靠渲染调度器的善意。**

2. **lazy 永远配叶子边界**。每个 `lazy()` 都在最小作用域包 `ScopedErrorBoundary → Suspense`（如 `UsageChartLoadBoundary.tsx`），fallback 是就地的空态组件；注释还给出取舍依据——"一句「加载中」在 200ms 的懒加载里只会闪一下"（`ArtifactChart.tsx`），短加载用无文字占位，重预览用静态纹理代替空白（`PreviewPane.tsx`）。

3. **门禁只拦首屏，旧数据优于空白**（`WorkspaceGroupedTasksSection.tsx`）：

   > 保留 initialized 门禁但隐藏主体，数据就绪后再一次性展示权威列表。门禁只该拦首屏……分组整棵子树随之卸载再重挂载——这就是「左侧分组列表整块闪一下」。画过一次列表后一律继续渲染，旧数据优于空白。

   由单调 `hasPaintedOnce` latch 保证，可单测。

4. **两级错误边界收敛白屏**。根级 `AppErrorBoundary` 注释直言："一旦某个 Provider 或页面组件抛错，React 会整棵树卸载，用户看到的就只剩一张白屏"；局部 `ScopedErrorBoundary` 让 sidebar/chat/terminal/settings "各自失败各自恢复"，`resetKeys` 在切换时自动复位错误状态。

5. **数据与 Suspense 严格分工**：Suspense 只服务 `lazy` 代码分割；数据加载一律显式布尔，且 loading 只出现在空槽位——列表刷新永不闪 spinner。

6. **HTML 启动壳与 React 接手的连续性**：启动动画结束与 React commit 双条件才撤壳、复用同一 SVG 保证视觉连续（desktop `index.html` / `RootStartupLoading.tsx`），对应 Wenyi Desktop 的 `Bootstrap` 运行时门（做法一致，Wenyi 已具备）。

### 2.3 Codex 可迁移的模式（无 Web UI，仅模式层）

- **shell 优先可交互**：启动 I/O 期间先渲染可编辑的 composer 草稿，能力就绪后解锁（`startup_draft.rs`）——对应"首屏先出 shell，数据就绪再填内容"。
- **导航原子换页**：切会话时旧 transcript 保留在屏、新历史缓冲后一次 draw 提交（`InitialHistoryReplayBuffer`）——React 世界里 transition 的意图，但 Codex 用显式状态机实现，不依赖框架默认行为。
- **错误带内呈现**：非致命错误渲染为 transcript 内的一条通知，绝不整屏替换。
- **集中式 motion**：加载指示/闪烁统一在 `motion.rs`，reduced-motion 在 API 边界强制降级——对应把 loading 原语收敛为共享组件。

### 2.4 Wenyi 现状差距清单

| # | 差距 | 风险 |
|---|---|---|
| G1 | 路由信息三处手工维护（路由表 / 预热数组 / 导航链接） | 漏改任一处 → 闪屏或导航缺失回归 |
| G2 | 路由挂起只有约定"边界在 shell 内"，无静态检查 | 大改时把 Suspense 挪回去无人拦截 |
| G3 | 嵌套 lazy 无强制规则（须登记预热或带局部边界） | 新增平台能力组件重现"页内闪" |
| G4 | 单一顶层错误边界：页面崩溃 = 全屏错误页 + 整页 reload | 局部故障爆炸半径过大，reload 丢失编辑态 |
| G5 | 加载态约定散落（quiet gate / `isLoading` / `ErrorNotice` 各页自定） | 新页面随手 `isLoading` 换整页 spinner，刷新闪烁 |
| G6 | E2E 未断言"shell 在导航中保持同一 DOM 节点" | 保旧行为可被无声破坏 |
| G7 | route chunk 只断言 4 个，通配重定向、其余 9 条路由无测试 | 路由清单不完整时无感知 |

## 3. 优化方案

原则：**保留 react-router URL 路由**（Wenyi Web/Desktop 都依赖可分享的项目 URL，ZCode 的 state 路由不适用），在 library 模式内完成以下改造；ZCode 的可执行架构治理思路用于护栏。

### P0 — 护栏先行（防回归，改动最小）

1. **新增 `packages/ui/tests/routing-rules.test.mjs`**（并入 `pnpm test:frontend-boundaries` 执行链），静态断言：
   - `packages/ui/src` 中 `Suspense` 只允许出现在白名单位置（`AppLayout` 的 Outlet、各页面/组件文件内部），**禁止出现在包住 `Routes`/`AppLayout` 的层级**（解析 `App.tsx` 的 JSX 结构，而非字符串 grep）；
   - 每个 `lazy(` 调用必须满足二选一：其 loader 登记在启动预热清单中，或**同文件**存在包裹它的 `<Suspense>`；
   - `App.tsx` 路由表条目数与 `routeLoaders` 条目数一致（过渡期检查，P1 清单化后改为清单完整性检查）。
2. **E2E 增加"shell 永不卸载"断言**：导航前后在页面上下文保存 `#sidebar-navigation` 的元素句柄，断言仍是同一节点（`isConnected && handle === el`）；对 Web、Desktop 各写一条。
3. **补齐路由测试**（G7）：13 条路由 chunk 全部断言（替换现有 4 条硬编码），通配 `* → /` 重定向补测。

### P1 — 路由清单化 + 统一 `RouteGate`（框架主体改造）

1. **`routeManifest` 单一事实源**（新文件如 `packages/ui/src/routes/manifest.tsx`）：

   ```ts
   interface RouteEntry {
     path: string;                 // "/projects/:pid/settings"
     element: ReactNode;           // lazy 页面
     loader: () => Promise<unknown>; // 代码分割 loader（预热与预取共用）
     nav?: { label: MessageKey; icon: LucideIcon; group: "global" | "project"; order: number; bookOnly?: boolean; srtOnly?: boolean };
   }
   ```

   由清单生成：① `<Routes>` 路由表；② `Navigation.tsx` 的全局/项目导航链接（取代手写数组）；③ 启动预热 `routeLoaders`；④ （P2）意图预取。新增/删除路由只改一处，P0 的清单完整性检查随之生效。
2. **`RouteGate` 组件**（新文件如 `packages/ui/src/routes/RouteGate.tsx`），结构固定为：

   ```tsx
   <RouteErrorBoundary resetKey={pathname}>   {/* 局部错误边界：只替换内容区，重试不整页 reload */}
     <Suspense fallback={<RoutePlaceholder />}> {/* quiet gate：复用 .route-placeholder，无文字 */}
       {element}
     </Suspense>
   </RouteErrorBoundary>
   ```

   - 取代现有的"顶层 `RouteBoundary` + 顶层 Suspense + AppLayout 内 Suspense"三层结构：**每条路由自带边界**，故障爆炸半径收敛到内容区（对应 ZCode `ScopedErrorBoundary` 的 scope/resetKeys 语义，resetKey 用 pathname/pid，切页自动复位）；
   - 错误页动作从 `location.reload()` 改为**重置该边界**（保存的编辑态不丢），保留整页 reload 作为兜底按钮；
   - 顶层只保留一个全局 `AppErrorBoundary`（对应 ZCode 根级边界，兜 Provider/壳层崩溃）。
3. **复用型 `LazyBoundary`**：抽取 ZCode `UsageChartLoadBoundary` 同款组件（错误边界外套 Suspense），替换 `Accounting.tsx`、`ProviderSettings.tsx` 的手写组合，作为今后一切组件级 lazy 的标准包装。

### P2 — 意图预取

- 清单驱动：`NavigationLink` 统一加 `onPointerEnter`/`focus` 触发 `loader()` 预取（React Query 的数据预取可后续按页评估，先做 chunk 预取）；
- 启动预热保留但改为遍历清单；冷启动后用户首次导航基本命中内存，quiet gate 退化为理论兜底。

### P3 — 加载态分级与数据层约定（文档 + 清理）

在 [architecture](../architecture.md)（或本文档追加）固化四级约定，逐页对照清理：

| 级别 | 场景 | 做法 |
|---|---|---|
| L0 quiet gate | 路由 chunk 未就绪 | `RouteGate` 占位，**无文字**、只拦首屏、shell 常驻 |
| L1 区块 inline | 区块首次取数 | 行内 loading/空态组件，**loading 只渲染在空槽位**；已有数据的刷新沿用旧数据（react-query 默认行为，禁止 `isLoading` 整块替换） |
| L2 长任务 | 导出、翻译进度 | `role="status"` 进度提示（现有事件流） |
| L3 错误 | 任意级失败 | 局部 `ErrorNotice`/`RouteErrorBoundary`，永不整屏白 |

- 硬规则沿用 ZCode 注释原话精神：**"门禁只拦首屏"**（单调 latch，防 gate 反复开合引起子树重挂载）、**"旧数据优于空白"**、**短加载占位不说话**。
- 现有页面按此表排查（`Dashboard`、`ReviewPage`、`ContentsPage` 等的 `isLoading` 分支是否会造成有数据时闪烁）。

### P4 — 可选项：数据路由迁移（评估结论：暂不推荐）

react-router 数据路由（`RouterProvider` + `errorElement`/`loader`）在错误隔离上与 `RouteGate` 同构，但会引入 loader 与 react-query 的双层数据流，迁移面大、收益被 P1 覆盖。**保持 library 模式**；若未来需要导航级数据预取再重新评估。

## 4. 分阶段路线图与验证

| 阶段 | 内容 | 交付 | 验证（AGENTS.md 前端链） |
|---|---|---|---|
| P0 | 护栏：routing-rules 静态检查、shell 同节点断言、13 chunk + 通配测试 | 测试代码 + 少量断言改动 | 两端 typecheck/build、`pnpm test:frontend-boundaries`、两端受影响 e2e |
| P1 | `routeManifest` + `RouteGate` + `LazyBoundary` + 全局 `AppErrorBoundary` | 框架改造，页面零行为变化 | 同上 + 全量 e2e（路由结构变动，两端全跑）；真机 CDP 冷/热导航复测（沿用本次方法） |
| P2 | 清单驱动预取 + 启动预热并入 | `NavigationLink` 预取、manifest 遍历 | 同 P1 + 手动冷启动验证首导航无 gate |
| P3 | 四级加载态约定落文档 + 逐页清理 | 文档 + 页面调整 | 每页改动跑对应 e2e |
| P4 | 数据路由评估（默认不做） | 决策记录 | — |

依赖关系：P0 可独立先行（纯测试）；P1 是大改 UI 的地基，建议在页面级大改**之前**完成；P2/P3 可与页面大改并行。

## 5. 参考索引

- ZCode 保旧与防白屏注释：`V:\zcode-src\packages\ui\src\root\RootWorkspaceContent.tsx`、`root\useWorkspaceShellLifecycle.ts`、`ErrorBoundary.tsx`、`settings\usage-stats\UsageChartLoadBoundary.tsx`、`WorkspaceGroupedTasksSection.tsx`（首屏门禁 latch）
- ZCode 结构治理：`V:\zcode-src\architecture-policy.yaml`、`scripts\architecture\`、`knip.json`
- Codex 模式：`V:\codex\codex-rs\tui\src\startup_draft.rs`（shell 优先）、`app.rs` `InitialHistoryReplayBuffer`（原子换页）、`motion.rs`（集中加载指示）
- 本次事故修复：commit `3c42566`（shell 内嵌边界 + 预热 + 凭据局部边界）、`3dca213`（边界测试 Windows 修复）
