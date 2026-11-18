<!-- 本页由 scripts/style/tokens.mjs 从 packages/ui/tailwind.preset.js 与
packages/ui/src/index.css 生成；重新生成请运行 pnpm gen:tokens，请勿手改。 -->

# 设计令牌

共享 UI 采用三层令牌体系：原始刻度位于 Tailwind preset（packages/ui/tailwind.preset.js），
语义色与海拔位于 packages/ui/src/index.css 的 CSS 变量，组件通过 Tailwind 工具类与 cva
variant 消费两者。本页由上述来源生成；当本页与源不一致时，pnpm check:tokens 会失败。

## 颜色

语义色是 OKLCH 通道三元组，以 oklch(var(--token)) 消费；.dark 类直接更换取值，工具类保持不变。
状态色以基础色搭配 -foreground 变体。亮色主题的状态标签使用清新的浅色底、清晰彩色文字
与浅色细描边：status-success 用绿色，status-warning 用琥珀色，status-danger 用红色，
status-info 用蓝色。暗色主题保留柔和、低饱和的配色。两种主题均使用紧凑小圆角和清晰文字；
中性标签复用 secondary、secondary-foreground 和 border。状态配色独立于操作与警示色，
调整状态标签不会改变删除按钮或内联错误提示。
弹窗通知跟随应用主题（包括跟随系统）并复用状态配色，普通通知使用卡片色。
统计条与对应图例使用 chart-1 至 chart-6，轨道使用 chart-track；暗色主题采用更低饱和的色板。

| CSS 变量 | Tailwind | 亮色 | 暗色 |
| --- | --- | --- | --- |
| `--background` | `background` | `oklch(0.9795 0 0)` | `oklch(0.1999 0 0)` |
| `--foreground` | `foreground` | `oklch(0.1405 0.0044 285.82)` | `oklch(0.7982 0 0)` |
| `--card` | `card` | `oklch(1 0 0)` | `oklch(0.2354 0 0)` |
| `--card-foreground` | `card-foreground` | `oklch(0.1405 0.0044 285.82)` | `oklch(0.7982 0 0)` |
| `--card-hover` | `card-hover` | `oklch(0.972 0 0)` | `oklch(0.296 0 0)` |
| `--primary` | `primary` | `oklch(0.2103 0.0059 285.88)` | `oklch(0.861 0 0)` |
| `--primary-foreground` | `primary-foreground` | `oklch(0.9848 0 0)` | `oklch(0.2156 0 0)` |
| `--primary-hover` | `primary-hover` | `oklch(0.305 0.0059 285.88)` | `oklch(0.9255 0 0)` |
| `--secondary-hover` | `secondary-hover` | `oklch(0.953 0.0013 286.38)` | `oklch(0.33 0 0)` |
| `--secondary` | `secondary` | `oklch(0.9676 0.0013 286.38)` | `oklch(0.277 0 0)` |
| `--secondary-foreground` | `secondary-foreground` | `oklch(0.2103 0.0059 285.88)` | `oklch(0.7982 0 0)` |
| `--muted` | `muted` | `oklch(0.9676 0.0013 286.38)` | `oklch(0.277 0 0)` |
| `--muted-foreground` | `muted-foreground` | `oklch(0.5519 0.0137 285.94)` | `oklch(0.6409 0 0)` |
| `--accent` | `accent` | `oklch(0.972 0 0)` | `oklch(0.296 0 0)` |
| `--accent-foreground` | `accent-foreground` | `oklch(0.2103 0.0059 285.88)` | `oklch(0.7982 0 0)` |
| `--destructive` | `destructive` | `oklch(0.6368 0.2078 25.33)` | `oklch(0.3959 0.1331 25.72)` |
| `--destructive-foreground` | `destructive-foreground` | `oklch(0.9848 0 0)` | `oklch(0.9848 0 0)` |
| `--warning` | `warning` | `oklch(0.5542 0.1447 49.16)` | `oklch(0.4697 0.1224 46.71)` |
| `--warning-foreground` | `warning-foreground` | `oklch(0.9848 0 0)` | `oklch(0.9848 0 0)` |
| `--info` | `info` | `oklch(0.5061 0.1156 240.36)` | `oklch(0.438 0.0988 240.83)` |
| `--info-foreground` | `info-foreground` | `oklch(0.9848 0 0)` | `oklch(0.9848 0 0)` |
| `--border` | `border` | `oklch(0.9197 0.004 286.32)` | `oklch(0.3171 0 0)` |
| `--input` | `input` | `oklch(0.9197 0.004 286.32)` | `oklch(0.2967 0 0)` |
| `--ring` | `ring` | `oklch(0.2103 0.0059 285.88)` | `oklch(0.683 0 0)` |
| `--status-success` | `status-success` | `oklch(0.9836 0.0162 155.55)` | `oklch(0.29 0.0168 167)` |
| `--status-success-foreground` | `status-success-foreground` | `oklch(0.5248 0.1373 149.83)` | `oklch(0.8104 0.0381 165.56)` |
| `--status-success-border` | `status-success-border` | `oklch(0.9246 0.0811 155.98)` | `oklch(0.4167 0.0287 166.64)` |
| `--status-warning` | `status-warning` | `oklch(0.9866 0.0218 95.28)` | `oklch(0.2896 0.0176 77.93)` |
| `--status-warning-foreground` | `status-warning-foreground` | `oklch(0.5407 0.1186 65.54)` | `oklch(0.8087 0.0462 81.33)` |
| `--status-warning-border` | `status-warning-border` | `oklch(0.9089 0.1116 91.59)` | `oklch(0.4166 0.0311 77.76)` |
| `--status-danger` | `status-danger` | `oklch(0.9678 0.0142 17.4)` | `oklch(0.2895 0.0235 23.3)` |
| `--status-danger-foreground` | `status-danger-foreground` | `oklch(0.5314 0.1989 27.39)` | `oklch(0.789 0.0484 23)` |
| `--status-danger-border` | `status-danger-border` | `oklch(0.8513 0.0746 18.73)` | `oklch(0.4166 0.0472 23.82)` |
| `--status-info` | `status-info` | `oklch(0.9716 0.0136 255.03)` | `oklch(0.2864 0.0181 248.46)` |
| `--status-info-foreground` | `status-info-foreground` | `oklch(0.484 0.178 260.15)` | `oklch(0.7897 0.031 248.22)` |
| `--status-info-border` | `status-info-border` | `oklch(0.8605 0.0668 255.25)` | `oklch(0.4114 0.0323 248.62)` |
| `--popover` | `popover` | `oklch(1 0 0)` | `oklch(0.277 0 0)` |
| `--popover-foreground` | `popover-foreground` | `oklch(0.1405 0.0044 285.82)` | `oklch(0.7982 0 0)` |
| `--card-pressed` | `card-pressed` | `oklch(var(--card))` | `oklch(var(--card))` |
| `--secondary-pressed` | `secondary-pressed` | `oklch(var(--secondary))` | `oklch(var(--secondary))` |
| `--ink` | - | `oklch(0.16 0 0)` | `oklch(1 0 0)` |
| `--hover` | `hover` | `oklch(var(--ink) / 0.05)` | `oklch(var(--ink) / 0.08)` |
| `--selected` | `selected` | `oklch(var(--ink) / 0.08)` | `oklch(var(--ink) / 0.12)` |
| `--surface` | `surface` | `oklch(var(--ink) / 0.03)` | `oklch(var(--ink) / 0.03)` |
| `--text-placeholder` | `placeholder` | `oklch(var(--ink) / 0.35)` | `oklch(var(--ink) / 0.3)` |
| `--warning-text` | `warning-text` | `oklch(0.5542 0.1447 49.16)` | `oklch(0.7984 0.1424 77.38)` |
| `--chart-track` | `chart-track` | `oklch(var(--muted))` | `oklch(0.3211 0 0)` |
| `--chart-1` | `chart-1` | `oklch(0.8273 0.1014 230.38)` | `oklch(0.7084 0.0475 229.87)` |
| `--chart-2` | `chart-2` | `oklch(0.7686 0.1646 70.11)` | `oklch(0.729 0.0823 80.55)` |
| `--chart-3` | `chart-3` | `oklch(0.5875 0.1388 241.95)` | `oklch(0.6203 0.0841 252.43)` |
| `--chart-4` | `chart-4` | `oklch(0.8688 0.0198 252.85)` | `oklch(0.6371 0.0273 258.37)` |
| `--chart-5` | `chart-5` | `oklch(0.7038 0.123 182.48)` | `oklch(0.6658 0.0835 177.72)` |
| `--chart-6` | `chart-6` | `oklch(0.5547 0.0407 257.44)` | `oklch(0.6728 0.0304 267.1)` |
| `--diff-removed-bg` | `diff-removed` | `oklch(0.9355 0.031 17.72)` | `oklch(0.2576 0.0886 26.05)` |
| `--diff-removed-foreground` | `diff-removed-foreground` | `oklch(0.3959 0.1331 25.72)` | `oklch(0.8844 0.0593 18.34)` |
| `--diff-added-bg` | `diff-added` | `oklch(0.9622 0.0437 156.72)` | `oklch(0.2664 0.0628 152.95)` |
| `--diff-added-foreground` | `diff-added-foreground` | `oklch(0.3926 0.0897 152.53)` | `oklch(0.925 0.0805 156)` |
| `--border-subtle` | `subtle` | `oklch(var(--ink) / 0.055)` | `oklch(var(--ink) / 0.065)` |
| `--primary-pressed` | `primary-pressed` | `oklch(var(--primary))` | `oklch(var(--primary))` |

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
| `shadow-focus-ring` | `0 0 0 1px oklch(var(--ring))` | `0 0 0 1px oklch(var(--ring))` | 键盘焦点光环，可与海拔叠加 |
| `shadow-raised` | `inset 0 1px 0 rgba(255, 255, 255, 1), 0 -1px 3px -1px rgba(24, 26, 32, 0.16), 0 1px 2px rgba(24, 26, 32, 0.07), 0 3px 6px -3px rgba(24, 26, 32, 0.14)` | `inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 1px 2px rgba(0, 0, 0, 0.35), 0 2px 6px rgba(0, 0, 0, 0.40)` | 实体按键静置态 |
| `shadow-raised-hover` | `inset 0 1px 0 rgba(255, 255, 255, 1), 0 -1px 4px -1px rgba(24, 26, 32, 0.20), 0 2px 4px rgba(24, 26, 32, 0.09), 0 5px 10px -4px rgba(24, 26, 32, 0.18)` | `inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 2px 6px rgba(0, 0, 0, 0.42), 0 6px 16px rgba(0, 0, 0, 0.45)` | 实体按键悬停态 |
| `shadow-pressed` | `inset 0 2px 4px rgba(24, 26, 32, 0.14), inset 0 -1px 0 rgba(255, 255, 255, 0.55), 0 1px 1px rgba(24, 26, 32, 0.05)` | `inset 0 3px 7px rgba(0, 0, 0, 0.62), inset 0 -1px 0 rgba(255, 255, 255, 0.06)` | 按压下沉态 |
| `shadow-raised-invert` | `inset 0 1px 0 rgba(255, 255, 255, 0.16), inset 0 -1px 0 rgba(0, 0, 0, 0.30), 0 1px 2px rgba(24, 26, 32, 0.28), 0 3px 7px -3px rgba(24, 26, 32, 0.22)` | `inset 0 1px 0 rgba(255, 255, 255, 1), inset 0 -1px 0 rgba(0, 0, 0, 0.16), 0 1px 3px rgba(0, 0, 0, 0.45), 0 4px 10px -4px rgba(0, 0, 0, 0.50)` | 实心键静置态（取自相反极） |
| `shadow-raised-invert-hover` | `inset 0 1px 0 rgba(255, 255, 255, 0.21), inset 0 -1px 0 rgba(0, 0, 0, 0.34), 0 2px 4px rgba(24, 26, 32, 0.32), 0 6px 14px -5px rgba(24, 26, 32, 0.30)` | `inset 0 1px 0 rgba(255, 255, 255, 1), inset 0 -1px 0 rgba(0, 0, 0, 0.20), 0 2px 6px rgba(0, 0, 0, 0.50), 0 8px 20px -6px rgba(0, 0, 0, 0.60)` | 实心键悬停态 |
| `shadow-pressed-invert` | `inset 0 1px 0 rgba(255, 255, 255, 0.06),
      inset 0 4px 10px -1px rgba(0, 0, 0, 0.95),
      inset 0 10px 18px -8px rgba(0, 0, 0, 0.85),
      inset 0 -2px 0 rgba(255, 255, 255, 0.22)` | `inset 0 3px 7px rgba(0, 0, 0, 0.30), inset 0 -1px 0 rgba(255, 255, 255, 0.90)` | 实心键按压态 |
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
- 组件只用语义工具类（如 bg-status-success、text-warning-text）；原始 Tailwind 色板类
  属于样式护栏违规。
- 需要叠加海拔与焦点光环时，使用列出变量的任意值，例如
  shadow-[var(--shadow-e2),var(--shadow-focus-ring)]。
- 禁用 transition-all；只过渡 transform、opacity、box-shadow、background-color 或 color；
  hover/focus/active 状态不得改变布局属性。
- 暗色模式基于 .dark 类：令牌自动翻转，组件不得硬编码暗色色板。
- 修改令牌时编辑 packages/ui/src/index.css 或 packages/ui/tailwind.preset.js，运行
  pnpm gen:tokens 并提交再生成页面；CI 通过 pnpm check:tokens 强制同步。
