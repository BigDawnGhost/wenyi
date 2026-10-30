# 页面加载态与错误呈现约定

适用范围：`packages/ui` 的共享页面与组件（Web / Desktop 两端一致）。本文是[前端框架对比与优化计划](frontend-framework.md) P3 的落地细则：大改新页面按本文编写，改完后按文末验收清单复查。

结构性约束（Suspense 位置、lazy 配对、manifest 完整性）已由 `packages/ui/tests/routing-rules.test.mjs` 静态强制，不在此重复。

## 四级加载态

| 级别 | 场景 | 做法 | 禁止 |
|---|---|---|---|
| L0 路由占位 | 路由 chunk 未就绪 | `RouteGate` 自动显示 quiet gate（无文字、shell 常驻），页面代码不做事 | 页面自行渲染整页 loading；在 shell 之上加 Suspense |
| L1 区块取数 | 区块首次请求数据 | loading/空态只渲染在**空槽位**；有数据后刷新**继续显示旧数据** | 用 `isLoading` 把已有内容整块换成 spinner/骨架 |
| L2 长任务 | 导出、翻译、分析进行中 | `role="status"` 的行内进度或 toast（沿用现有事件流模式） | 阻塞整页或锁死导航 |
| L3 错误 | 任意级失败 | 区块错误行内 `ErrorNotice`；路由错误由 `RouteGate` 兜住；shell 错误由 `AppErrorBoundary` 兜住 | 页面崩溃白屏；把 `location.reload()` 当常规恢复 |

## 数据层三条铁律

1. **首屏才显示 loading**。以 react-query 三态为准：
   - `isPending`（无数据）→ 空槽位显示行内 loading 或空态组件；
   - 已有 `data` + `isFetching` → 不换 UI，最多角落一个轻指示；
   - `error` + 有数据 → `ErrorNotice` 与数据并排（用户能边看数据边重试）；
   - `error` + 无数据 → 空槽位显示错误与重试按钮。
2. **门禁只拦首屏**。任何"首绘前隐藏主体"的派生门禁必须是单调的（画过一次就永远放行，参考 ZCode `hasPaintedOnce` latch 模式）——否则流式更新会让门禁反复开合，整棵子树卸载再重挂载，表现为"内容块周期性闪一下"。
3. **旧数据优于空白**。任何让"已有内容变空白或骨架"的更新都是缺陷，包括切换 tab、切工作区、后台刷新回来。

## 占位与文案

- **组件级懒加载占位不带文字**：200ms 量级的 chunk 加载里，一句"加载中"只会闪一下，比空白更糟。用 `LazyBoundary` 的安静 fallback（默认 `null`，或就地复用相邻内容，如凭据字段的环境变量框）。
- **区块首载可以用行内文案/spinner**（`role="status"`），因为那是用户在等第一份数据，不是等代码。
- 自带动画的占位复用 `.route-placeholder` / `.runtime-placeholder`（已处理 `prefers-reduced-motion`），不要新写动画。

## 错误呈现分工

- **区块级**：`ErrorNotice` 行内，带重试。
- **路由级**：不用写任何东西——`RouteGate` 的错误边界（resetKey=pathname）自动把失败限制在内容区，切到别的页面自动复位；其 fallback 自带 Reload（React.lazy 会缓存拒绝，只有重载页面能恢复 chunk 失败，这是保留 Reload 按钮的原因）。
- **shell/Provider 级**：`AppErrorBoundary` 全屏兜底。
- 页面代码**不要** `catch` 渲染错误后返回空白，也不要自行 `location.reload()`。

## 新页面接入清单（大改时逐条走）

1. 页面放 `features/<域>/`，在 `routes/manifest.tsx` 登记：路由条目 + loader +（如有）导航条目——**只改这一个文件**。
2. 组件级 `lazy(` 必须同文件包 `<LazyBoundary fallback={...}>`，或在启动时 `void load...()` 预热（护栏二选一强制）。
3. 数据态按上面三铁律接 react-query。
4. 页面顶层不加 `Suspense`（会被 routing-rules 拦下）。
5. 新增 i18n key 英文与中文同步（`i18n.spec.ts` 校验键一致性）。

## 验收清单（每个改完的页面过一遍）

- [ ] `pnpm test:frontend-boundaries` 6 项全过（含 Suspense/lazy/manifest 护栏）。
- [ ] 冷启动首载：空槽位 loading，shell 与导航全程可见。
- [ ] 慢速/重复刷新：已渲染内容不闪烁、不回退为骨架（铁律 1/3）。
- [ ] 断开 API：错误行内可见、导航仍可用、无白屏。
- [ ] 受影响时跑两端对应 e2e；侧栏链接计数与 `[data-route-pending]` 断言不许改动以迁就实现。
- [ ] `prefers-reduced-motion` 下占位无动画。

## 参考出处

- ZCode 首屏门禁与"旧数据优于空白"：`V:\zcode-src\packages\ui\src\WorkspaceGroupedTasksSection.tsx` 注释
- ZCode 局部错误边界（scope/resetKeys/variant）：`V:\zcode-src\packages\ui\src\ErrorBoundary.tsx`
- ZCode 短加载占位不说话：`V:\zcode-src\packages\ui\src\app-shell\workflow-artifacts\presets\ArtifactChart.tsx`
- 本仓库落地件：`packages/ui/src/routes/RouteGate.tsx`、`LazyBoundary.tsx`、`AppErrorBoundary.tsx`、`routes/manifest.tsx`
