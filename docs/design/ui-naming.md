# UI 定位与样式命名规范

目的：任何一块界面都能被稳定地指认（对话、测试、评审都用同一套名字），样式保持可预测。适用于 `packages/ui` 与两端宿主。

## 1. 结构定位：`data-slot`

**要指认"哪一块"，用 `data-slot="<区域>.<元素>"`（kebab-case）。** 这是稳定 API：测试与人工交流都以它为准，改样式不动它，重构布局时同步迁移它。当前清单：

| slot | 含义 |
|---|---|
| `sidebar` | 左侧栏整体（`<aside>`） |
| `sidebar.logo-row` | 顶部 logo 行（品牌 + 折叠按钮） |
| `sidebar.strata` | logo 行以下全部层级的容器（`#sidebar-navigation`） |
| `sidebar.action` | 主操作层（创建项目） |
| `sidebar.panels` | 全局面板导航层（项目列表，`aria-label=全局导航`） |
| `sidebar.region` | 项目导航滚动区（内含项目名 + 页面链接） |
| `sidebar.footer` | 底部层（设置） |
| `content` | 主内容区（`<Outlet>`/RouteGate 所在的 `<main>`） |
| `page.header` | 页头（标题 + 副标题 + 操作按钮） |
| `page.container` | 页面正文容器 |
| `card` / `card.header` / `card.title` / `card.content` | 卡片及其分部 |
| `button` | 按钮基础件 |

辅助定位手段（优先级从高到低）：

1. **`data-slot`** —— 结构块的主键；
2. **ARIA 角色 + 可访问名** —— `navigation[aria-label=全局导航]`、`main`、`heading` 等语义天然可定位；
3. **专用标记** —— `data-route-pending`（路由 quiet gate）、`data-runtime-state`（Desktop 运行时门）、`role=alert/status`。

**规则：新增可见区域必须声明其 `data-slot` 并更新本表；测试禁止依赖未登记的类名/层级结构做定位。**

## 2. class 命名

- **样式一律用 Tailwind 工具类内联**在元素上；不发明形如工具类的语义类（如 `sidebarBox`、`navItemActive`）。
- **确需自定义 CSS 的，用 kebab-case 的 `块-元素` 名**，并在下表登记为稳定名（当前全部）：

| 类名 | 用途 |
|---|---|
| `brand-wordmark` | 品牌文字（梦源宋体子集 + Georgia 回退） |
| `runtime-placeholder` | 全屏安静门（Desktop 运行时 pending/closing、顶层兜底） |
| `route-placeholder` | 内容区安静门（RouteGate 的路由占位） |
| `disclosure-summary` / `disclosure-chevron` | 折叠详情的原生 summary 样式 |

- **状态表达优先用工具类变体**（`disabled:`、`aria-*`、`data-*`），不新增状态类名。

## 3. 单位：px-only

- **长度一律 px**（根字号 16px 换算，视觉与原 rem 完全等价）：Tailwind 尺度在 `packages/ui/tailwind.preset.js` 统一声明（spacing/fontSize/lineHeight/letterSpacing/maxWidth/borderRadius/translate），自写 CSS 直接写 px。
- 视口单位 `vh/vw/dvh` 保留（高度门、弹层视野限制），百分比用于宽度/位移动态值。
- 已知例外：Tailwind preflight 原文仍带 3 处 em（`sub/sup` 位移、`code/kbd` 字号），已在 `index.css` 用同名 px/`inherit` 规则覆盖；preflight 无法按条关闭。

## 4. 内联 `style` 的豁免

仅允许**运行时动态值**：进度条 `width: %`、菜单 `left/top` 坐标、`contentVisibility` 等。静态视觉（字体、颜色、间距）必须落在类里——品牌字体是先例（曾内联，已迁移）。

## 5. 新增块接入清单

1. 声明 `data-slot`（结构块）并登记到本文档表；
2. 样式只用工具类；确需自定义 CSS → kebab-case + 登记；
3. 长度 px；视口/动态值按第 3、4 节豁免；
4. 定位类测试用 slot/ARIA，不用类名。
