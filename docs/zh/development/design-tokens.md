<!-- 本页由 scripts/style/tokens.mjs 从 packages/ui/tailwind.preset.js 与
packages/ui/src/index.css 生成；重新生成请运行 pnpm gen:tokens，请勿手改。 -->

# 设计令牌

共享 UI 采用三层令牌体系：原始刻度位于 Tailwind preset（packages/ui/tailwind.preset.js），
语义色与海拔位于 packages/ui/src/index.css 的 CSS 变量，组件通过 Tailwind 工具类与 cva
variant 消费两者。本页由上述来源生成；当本页与源不一致时，pnpm check:tokens 会失败。

## 颜色

语义色是 HSL 三元组，以 hsl(var(--token)) 消费；.dark 类直接更换取值，工具类保持不变。
状态色成对使用：基础色搭配 -foreground 变体，用于实色底上的前景内容。success、warning、
info 与 destructive 分别表达成功、警示、提示与失败/危险状态。

| CSS 变量 | Tailwind | 亮色 | 暗色 |
| --- | --- | --- | --- |
| `--background` | `background` | `hsl(240 20% 97%)` | `hsl(240 10% 3.9%)` |
| `--foreground` | `foreground` | `hsl(240 10% 3.9%)` | `hsl(0 0% 98%)` |
| `--card` | `card` | `hsl(0 0% 100%)` | `hsl(240 8% 9%)` |
| `--card-foreground` | `card-foreground` | `hsl(240 10% 3.9%)` | `hsl(0 0% 98%)` |
| `--primary` | `primary` | `hsl(240 5.9% 10%)` | `hsl(0 0% 98%)` |
| `--primary-foreground` | `primary-foreground` | `hsl(0 0% 98%)` | `hsl(240 5.9% 10%)` |
| `--secondary` | `secondary` | `hsl(240 4.8% 95.9%)` | `hsl(240 3.7% 15.9%)` |
| `--secondary-foreground` | `secondary-foreground` | `hsl(240 5.9% 10%)` | `hsl(0 0% 98%)` |
| `--muted` | `muted` | `hsl(240 4.8% 95.9%)` | `hsl(240 3.7% 15.9%)` |
| `--muted-foreground` | `muted-foreground` | `hsl(240 3.8% 46.1%)` | `hsl(240 5% 64.9%)` |
| `--accent` | `accent` | `hsl(240 4.8% 95.9%)` | `hsl(240 3.7% 15.9%)` |
| `--accent-foreground` | `accent-foreground` | `hsl(240 5.9% 10%)` | `hsl(0 0% 98%)` |
| `--destructive` | `destructive` | `hsl(0 84.2% 60.2%)` | `hsl(0 62.8% 30.6%)` |
| `--destructive-foreground` | `destructive-foreground` | `hsl(0 0% 98%)` | `hsl(0 0% 98%)` |
| `--success` | `success` | `hsl(161 94% 30%)` | `hsl(163 88% 20%)` |
| `--success-foreground` | `success-foreground` | `hsl(0 0% 98%)` | `hsl(0 0% 98%)` |
| `--warning` | `warning` | `hsl(26 90% 37%)` | `hsl(23 82% 31%)` |
| `--warning-foreground` | `warning-foreground` | `hsl(0 0% 98%)` | `hsl(0 0% 98%)` |
| `--info` | `info` | `hsl(200 96% 32%)` | `hsl(201 90% 27%)` |
| `--info-foreground` | `info-foreground` | `hsl(0 0% 98%)` | `hsl(0 0% 98%)` |
| `--border` | `border` | `hsl(240 5.9% 90%)` | `hsl(240 3.7% 15.9%)` |
| `--input` | `input` | `hsl(240 5.9% 90%)` | `hsl(240 3.7% 15.9%)` |
| `--ring` | `ring` | `hsl(240 5.9% 10%)` | `hsl(240 4.9% 83.9%)` |
| `--ink` | - | `hsl(13 13 13)` | `hsl(255 255 255)` |
| `--hover` | `hover` | `hsl(var(--ink) / 0.05)` | `hsl(var(--ink) / 0.08)` |
| `--selected` | `selected` | `hsl(var(--ink) / 0.08)` | `hsl(var(--ink) / 0.12)` |
| `--surface` | `surface` | `hsl(var(--ink) / 0.03)` | `hsl(var(--ink) / 0.03)` |
| `--text-placeholder` | `placeholder` | `hsl(var(--ink) / 0.35)` | `hsl(var(--ink) / 0.3)` |
| `--warning-text` | `warning-text` | `hsl(26 90% 37%)` | `hsl(45.9 96.7% 64.5%)` |
| `--chart-1` | `chart-1` | `hsl(199.4 95.5% 73.9%)` | `hsl(199.4 95.5% 73.9%)` |
| `--chart-2` | `chart-2` | `hsl(37.7 92.1% 50.2%)` | `hsl(37.7 92.1% 50.2%)` |
| `--chart-3` | `chart-3` | `hsl(200.4 98% 39.4%)` | `hsl(198.4 93.2% 59.6%)` |
| `--chart-4` | `chart-4` | `hsl(212.7 26.8% 83.9%)` | `hsl(212.7 26.8% 83.9%)` |
| `--chart-5` | `chart-5` | `hsl(173.4 80.4% 40%)` | `hsl(172.5 66% 50.4%)` |
| `--chart-6` | `chart-6` | `hsl(215.4 16.3% 46.9%)` | `hsl(215 20.2% 65.1%)` |
| `--diff-removed-bg` | `diff-removed` | `hsl(0 93.3% 94.1%)` | `hsl(0 74.7% 15.5%)` |
| `--diff-removed-foreground` | `diff-removed-foreground` | `hsl(0 62.8% 30.6%)` | `hsl(0 96.3% 89.4%)` |
| `--diff-added-bg` | `diff-added` | `hsl(140.6 84.2% 92.5%)` | `hsl(144.9 80.4% 10%)` |
| `--diff-added-foreground` | `diff-added-foreground` | `hsl(143.8 61.2% 20.2%)` | `hsl(141 78.9% 85.1%)` |
| `--border-subtle` | `subtle` | `hsl(var(--ink) / 0.055)` | `hsl(var(--ink) / 0.065)` |

## 间距

统一的 4px 刻度覆盖间距、尺寸与位移；组件从该表取键（含半阶），不得自造数值。

| 键 | 值 |
| --- | --- |
| `0` | `0px` |
| `px` | `1px` |
| `0.5` | `2px` |
| `1` | `4px` |
| `1.5` | `6px` |
| `2` | `8px` |
| `2.5` | `10px` |
| `3` | `12px` |
| `3.5` | `14px` |
| `3.75` | `15px` |
| `4` | `16px` |
| `4.5` | `18px` |
| `5` | `20px` |
| `6` | `24px` |
| `7` | `28px` |
| `8` | `32px` |
| `9` | `36px` |
| `10` | `40px` |
| `11` | `44px` |
| `12` | `48px` |
| `14` | `56px` |
| `16` | `64px` |
| `20` | `80px` |
| `24` | `96px` |
| `28` | `112px` |
| `32` | `128px` |
| `36` | `144px` |
| `40` | `160px` |
| `44` | `176px` |
| `48` | `192px` |
| `52` | `208px` |
| `56` | `224px` |
| `60` | `240px` |
| `64` | `256px` |
| `72` | `288px` |
| `80` | `320px` |
| `96` | `384px` |
| `112` | `448px` |
| `128` | `512px` |
| `144` | `576px` |
| `160` | `640px` |
| `176` | `704px` |
| `192` | `768px` |
| `208` | `832px` |
| `224` | `896px` |
| `240` | `960px` |
| `256` | `1024px` |
| `288` | `1152px` |
| `320` | `1280px` |
| `384` | `1536px` |

## 圆角

--radius 为 8px，其余由 preset 派生；元素越大，圆角越大。

| 键 | 值 | 解析值 |
| --- | --- | --- |
| `DEFAULT` | `4px` | `4px` |
| `lg` | `var(--radius)` | `8px` |
| `md` | `calc(var(--radius) - 2px)` | `6px` |
| `sm` | `calc(var(--radius) - 4px)` | `4px` |
| `xl` | `12px` | `12px` |
| `2xl` | `16px` | `16px` |
| `3xl` | `24px` | `24px` |

## 海拔

海拔取代装饰性描边承担结构：凹井（e0）、静置面（e1-e2）、hover 抬升（e3）与浮层（e4）。
每个工具类映射到一条随主题切换的完整 box-shadow 阴影列表。

| 工具类 | 亮色 | 暗色 | 用途 |
| --- | --- | --- | --- |
| `shadow-e0` | `inset 0 1px 2px rgba(24, 26, 32, 0.07), 0 0 0 1px rgba(24, 26, 32, 0.05)` | `inset 0 1px 2px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.06)` | 凹井：输入件、进度轨道、勾选框底 |
| `shadow-e1` | `inset 0 1px 0 rgba(255, 255, 255, 0.65), 0 1px 2px rgba(24, 26, 32, 0.04), 0 0 0 1px rgba(24, 26, 32, 0.06)` | `inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 0 0 1px rgba(255, 255, 255, 0.07)` | 静置面：卡片、侧栏、面板 |
| `shadow-e2` | `inset 0 1px 0 rgba(255, 255, 255, 0.65), 0 1px 3px rgba(24, 26, 32, 0.06), 0 0 0 1px rgba(24, 26, 32, 0.07)` | `inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 2px 6px rgba(0, 0, 0, 0.35), 0 0 0 1px rgba(255, 255, 255, 0.08)` | 交互控件静置态 |
| `shadow-e3` | `inset 0 1px 0 rgba(255, 255, 255, 0.65), 0 4px 8px -2px rgba(24, 26, 32, 0.08), 0 12px 28px -12px rgba(24, 26, 32, 0.14), 0 0 0 1px rgba(24, 26, 32, 0.06)` | `inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 6px 16px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.09)` | hover 抬升态 |
| `shadow-e4` | `0 8px 16px -4px rgba(24, 26, 32, 0.08), 0 28px 56px -20px rgba(24, 26, 32, 0.22), 0 0 0 1px rgba(24, 26, 32, 0.06)` | `0 12px 32px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.09)` | 浮层：对话框、弹出层、toast |
| `shadow-focus-ring` | `0 0 0 1px hsl(var(--ring))` | `0 0 0 1px hsl(var(--ring))` | 键盘焦点光环，可与海拔叠加 |
| `shadow-input-hover` | `inset 0 1px 2px rgba(24, 26, 32, 0.09), 0 0 0 1px rgba(24, 26, 32, 0.16)` | `inset 0 1px 2px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.16)` |  |
| `shadow-sidebar` | `0 0 0 1px rgba(24, 26, 32, 0.05), 12px 0 32px -20px rgba(24, 26, 32, 0.18)` | `0 0 0 1px rgba(255, 255, 255, 0.06), 12px 0 32px -20px rgba(0, 0, 0, 0.5)` | 侧栏右缘：发丝线加柔影 |

## 动效

时长与缓动按标尺组合：进入用 duration-motion-normal ease-decelerate，退出用 accelerate，
hover 与按压反馈用 standard，侧栏保留专属节奏。

| 工具类 | 值 | 用途 |
| --- | --- | --- |
| `duration-motion-fast` | `120ms` | 按压、色彩与焦点变化 |
| `duration-motion-normal` | `200ms` | 标准过渡与进入 |
| `duration-motion-slow` | `320ms` | 大件表面：对话框、页面转场 |
| `duration-sidebar` | `280ms` | 侧栏折叠（保留的既有节奏） |

| 工具类 | 曲线 | 用途 |
| --- | --- | --- |
| `ease-standard` | `cubic-bezier(0.2, 0, 0, 1)` | 通用反馈 |
| `ease-decelerate` | `cubic-bezier(0.05, 0.7, 0.1, 1)` | 进入：快出缓收 |
| `ease-accelerate` | `cubic-bezier(0.3, 0, 0.8, 0.15)` | 退出：缓出快收 |
| `ease-sidebar` | `cubic-bezier(0.22, 1, 0.36, 1)` | 侧栏折叠曲线 |

## 用法约定

- 仅用 px：间距、字号与几何一律使用 px，不使用 em/rem。
- 组件只用语义工具类（如 bg-success、text-warning、border-info）；原始 Tailwind 色板类
  属于样式护栏违规。
- 需要叠加海拔与焦点光环时，使用列出变量的任意值，例如
  shadow-[var(--shadow-e2),var(--shadow-focus-ring)]。
- 禁用 transition-all；只过渡 transform、opacity、box-shadow、background-color 或 color；
  hover/focus/active 状态不得改变布局属性。
- 暗色模式基于 .dark 类：令牌自动翻转，组件不得硬编码暗色色板。
- 修改令牌时编辑 packages/ui/src/index.css 或 packages/ui/tailwind.preset.js，运行
  pnpm gen:tokens 并提交再生成页面；CI 通过 pnpm check:tokens 强制同步。
