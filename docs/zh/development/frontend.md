# 前端开发

Web 和 Desktop 共享页面与基础组件，不共享平台实现。本文规定两端的路由、加载、
无障碍和样式规则。宿主启动方式见 [Web 开发](web.md)与
[Desktop 开发](desktop.md)。

## 共享代码与宿主边界

共享功能位于 `packages/ui`，Web 拥有 `apps/web/src`，Desktop 拥有
`apps/desktop/frontend/src`。两端在渲染共享页面前配置
[平台服务契约](../../../packages/ui/src/platform/contracts.ts#L122)。

- 共享 UI 不得导入任一 app、检查原生全局变量，或用 `isDesktop` 一类标志猜测平台。
- 传输、进度订阅、偏好存储、文件操作及可选原生能力均经服务注入。共享 UI 不能直接
  调用 `fetch`、`new WebSocket`、`localStorage` 或 `sessionStorage`。
- Web 产物不包含原生启动、IPC 或凭据库 UI。Desktop 使用自己的适配，不依赖 Web 部署。
- Desktop 专属面板通过可选能力提供，不把实现放进共享层；Web 中缺失的能力不能留下
  无法使用的控件。
- API 变更同步后端 schema、共享生成类型及两端适配。契约变更后使用
  `pnpm gen:schema` 和 `pnpm check:schema` 生成并检查类型。

[平台边界检查](../../../packages/ui/tests/platform-boundaries.test.mjs#L17)
同时检查源码和构建产物。只检查源码不足以证明 Web 包中没有原生实现。

## 路由与常驻壳层

[路由清单](../../../packages/ui/src/routes/manifest.tsx#L27)是共享事实源，在同一模块
维护 `routeEntries`、`pageLoaders`、导航项及设置分区可用性。新增功能页、lazy loader
及导航项都在这里登记，不在两端另建路由表、预热列表或侧栏列表。

应用从清单生成 URL 路由，启动时预热页面 chunk，导航在鼠标进入及键盘聚焦时预取同一
loader。保留 URL 路由和通配重定向；路由别名可以共用 loader。设置分区根据能力显示，
不能假定两端有相同的原生面板。

导航过程中壳层保持挂载。
[路由门](../../../packages/ui/src/routes/RouteGate.tsx#L65)包在布局主内容区
**内部**的 outlet 外：

- `Suspense` fallback 是安静、无文字的 `.route-placeholder`，带
  `aria-busy="true"` 和 `data-route-pending`。
- 错误边界只替换路由内容，pathname 改变时复位。fallback 保留 Reload，因为 React
  lazy import 会缓存拒绝结果；不能声称仅重置边界即可重新加载失败的 chunk。
- 根 `AppErrorBoundary` 处理壳层/provider 故障。不要把路由门或 `Suspense`
  移到壳层之上。

组件级 lazy import 使用最小局部
[LazyBoundary](../../../packages/ui/src/routes/LazyBoundary.tsx#L3)，或明确预热
loader。默认 fallback 为 `null`，也可使用稳定的局部占位。
**它仅包装 Suspense，不包含错误边界。** 可选组件加载不能替换整页。

[路由规则](../../../packages/ui/tests/routing-rules.test.mjs#L28)将直接 `Suspense`
限制在 route/lazy gate，检查 lazy 配对及清单一致性。复用这些 gate，不在页面另加
`Suspense`。

## 加载与错误边界

Suspense 用于代码分割；查询加载态显式管理，属于正在等待数据的最小区域。

| 级别 | 场景 | 呈现 |
| --- | --- | --- |
| 路由代码 | 路由 chunk 尚未就绪 | 安静路由门，壳层/导航始终可见 |
| 首次数据 | 区块还没有数据 | 在该空槽位显示行内 loading 或空态 |
| 长任务 | 翻译、分析或导出进行中 | 沿用现有事件流，以行内进度或通知及 `role="status"` 呈现 |
| 故障 | 查询、操作或渲染失败 | 按归属范围使用行内错误、路由边界或根边界 |

查询遵守以下规则：

1. 无数据时，`isPending` 可显示行内 loading。已有数据且 `isFetching` 时继续显示内容，
   最多加一个轻量刷新指示。
2. 后台错误与**保留的数据并排**显示。无数据时，在空区域显示错误及重试操作。
   共享 [ErrorNotice](../../../packages/ui/src/components/ui/data.tsx#L5)负责 alert，
   重试控件由调用方提供。
3. 首绘门禁对当前数据身份必须单调：一旦展示内容，刷新不得反复隐藏/重挂载内容。
4. 只能保留属于对应身份的数据。切换 project/workspace/run 时，不得把上一项目的元数据、
   进度或编辑态当成新项目显示。保留壳层不意味着可以混用查询身份。

短暂的代码加载占位不闪现“加载中”。首次数据请求可使用本地化行内文案和
`role="status"`。复用 `.route-placeholder`/`.runtime-placeholder` 动画及其
`prefers-reduced-motion` 行为；不得为了动画延迟退出。

可恢复操作使用行内错误，不能 catch 渲染错误后返回白页。页面不能把
`location.reload()` 当作普通重试。长任务不能阻塞导航或替换整页。

## 无障碍与语义 slots

交互使用语义 HTML 和可访问名：有标签的导航、合适且不重复的 main 地标、有意义的
标题层级、有名称的按钮/字段、用于进度的 `role="status"` 和用于错误的
`role="alert"`。`data-slot` 是稳定的结构身份，不能替代无障碍语义。

结构区域使用 `data-slot="<区域>.<元素>"`，名称采用 kebab-case。调整外观时保持名称稳定，
结构迁移时明确同步。共享
[布局](../../../packages/ui/src/components/layout/AppLayout.tsx#L140)和基础组件定义以下
核心词汇：

| Slot | 含义 |
| --- | --- |
| `sidebar` | 整个侧栏 |
| `sidebar.logo-row` | 品牌/设置标题及折叠控件 |
| `sidebar.strata` | logo 行下的侧栏层级，`#sidebar-navigation` |
| `sidebar.action` | 主操作 |
| `sidebar.panels` | 全局导航 |
| `sidebar.region` | 可滚动的项目导航 |
| `sidebar.footer` | 底部导航/操作区 |
| `content` | 主路由内容区 |
| `page.header` | 页标题、副标题及操作 |
| `page.container` | 页正文 |
| `card`、`card.header`、`card.title`、`card.content` | 卡片及其分部 |
| `button` | 共享按钮基础组件 |

新增共享结构 slot 时同步此表。测试通过 slot 定位结构区域，通过 ARIA 角色和可访问名
定位交互，不依赖工具类或偶然的 DOM 嵌套。`data-route-pending` 和 Desktop
`data-runtime-state` 等专用标记表达生命周期，不表达外观。

## 样式

静态样式使用内联 Tailwind 工具类，不发明 `sidebarBox`、`navItemActive` 一类伪工具
语义类。状态优先使用 `disabled:`、`aria-*`、`data-*` 变体，不新增状态类名。

[Tailwind preset](../../../packages/ui/tailwind.preset.js#L3)以 **px** 定义间距、字号、
行高、字距、最大宽度、圆角及位移尺度。新增固定 CSS 长度和 arbitrary utility 使用
px，而非 rem/em。视口约束可用视口单位；相对尺寸、动态进度及位置可用百分比。
无单位比例不属于固定长度。

内联 `style` 只用于运行时值，如进度宽度、计算后的菜单坐标或 `contentVisibility`。
静态字体、颜色和间距必须放入类。

确需自定义 CSS 时，使用已登记的 kebab-case 名并保留在共享
[样式表](../../../packages/ui/src/index.css#L15)：

| 类名 | 用途 |
| --- | --- |
| `brand-wordmark` | 共享品牌字体 |
| `runtime-placeholder` | Desktop 运行时 pending/closing 门 |
| `route-placeholder` | 安静的内容区路由门 |
| `disclosure-summary`、`disclosure-chevron` | 原生折叠详情样式 |

这些是编写约定，不代表每个依赖或已有 arbitrary utility 都已完全采用 px。
两端保持共享主题 token 及减少动态效果行为，不通过把样式复制到 app 绕过共享规则。

## 弹窗与全局通知

使用共享 [Dialog](../../../packages/ui/src/components/ui/misc.tsx#L7)，即通过 Portal
挂载到 body 的 Radix Dialog。提供可访问标题、可选说明、合适的初始/返回焦点，
仅在工作流需要时禁止关闭。滚动保留在弹窗正文内，短窗口中操作按钮仍须可达。
Radix Select 的 Portal 选项必须能在弹窗上方使用。不要改用会以浏览器顶层覆盖选项的
原生 `showModal()`。共享背景遮罩使用 2px 模糊。

[NotificationToaster](../../../packages/ui/src/components/NotificationToaster.tsx#L5)
只在壳层挂载一次，位于可滚动路由内容之外。body Portal 的通知栈固定在视口右上角，
高于弹窗/下拉框，并带可用的关闭控件。异步反馈不跟随最后点击的按钮；滚动和无关点击
不能改变其位置。

使用共享 [toast 适配](../../../packages/ui/src/lib/toast.ts#L5)：成功默认一秒，
错误/警告十秒，信息五秒；调用方可在必要时覆盖时长。通知不能替代可操作的行内校验，
例如表单保存失败在通知消失后仍应保留于打开的表单中。

## 两端验证

使用 Node 22、pnpm 9，通过 `pnpm install --frozen-lockfile` 安装依赖。
尚无 Playwright Chromium 时执行 `pnpm -C apps/web exec playwright install chromium`。
从仓库根目录运行：

```bash
pnpm -C apps/web typecheck
pnpm -C apps/desktop/frontend typecheck
pnpm -C apps/web build
pnpm -C apps/desktop/frontend build
pnpm test:frontend-boundaries
pnpm -C apps/web test:e2e
pnpm -C apps/desktop/frontend test:e2e
git diff --check
```

边界检查必须在两端 build 后运行。局部变更运行受影响 E2E；路由/壳层变更应运行两端
完整套件。Playwright 配置分别在 4173（Web）和 4174（Desktop）启动本地 Vite 服务。
Desktop 浏览器套件 mock 原生 IPC，不启动 Tauri。可用
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定已有浏览器，`PLAYWRIGHT_WORKERS` 控制并发。
性能项目通过 `PLAYWRIGHT_PERF=1` 显式启用，与普通行为检查分开。

对每个受影响页面验证：

- 冷启动/慢速导航保留同一个仍连接的壳层/侧栏 DOM 节点，loading 留在所属区域，
  通配路由仍然正常。
- 后台刷新保留匹配身份的数据；错误可见，不清空内容或禁用导航。
- lazy widget 不替换整页；减少动态效果模式下占位不播放动画。
- 弹窗初始/返回焦点、Portal 下拉选项、行内错误和通知在两端的键盘操作、滚动及
  延迟响应下都正常。
- 英中 i18n key 保持一致；进度/事件断言使用实际的 project/run 身份。

检查聚焦当前可见行为，不记录固定测试数量或旧故障复现历史。浏览器检查不能证明原生
WebView 性能、系统对话框或发行包平台验收通过。
