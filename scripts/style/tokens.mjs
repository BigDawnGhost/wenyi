/** Generate/check the bilingual design token documentation from the live token sources. */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import preset from "../../packages/ui/tailwind.preset.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const mode = process.argv[2] ?? "--check";
if (!["--check", "--write"].includes(mode)) throw new Error("Use --check or --write.");

const css = readFileSync(join(root, "packages", "ui", "src", "index.css"), "utf8");
const backtick = String.fromCharCode(96);

function parseVariables(block) {
  const variables = {};
  for (const match of block.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) {
    variables["--" + match[1]] = match[2].trim();
  }
  return variables;
}

function themeBlock(selector) {
  const match = css.match(new RegExp(selector + "\\s*\\{([^}]*)\\}"));
  if (!match) throw new Error("Missing " + selector + " block in packages/ui/src/index.css.");
  return parseVariables(match[1]);
}

const light = themeBlock(":root");
const dark = themeBlock("\\.dark");

// Map each CSS variable back to the Tailwind color utility that exposes it.
const classByVariable = new Map();
for (const [key, value] of Object.entries(preset.theme.extend.colors)) {
  const entries = typeof value === "string" ? [["DEFAULT", value]] : Object.entries(value);
  for (const [sub, resolved] of entries) {
    const variable = String(resolved).match(/var\((--[\w-]+)\)/);
    if (variable) classByVariable.set(variable[1], sub === "DEFAULT" ? key : key + "-" + sub);
  }
}

const colorOrder = [
  "background", "foreground", "card", "card-foreground", "primary", "primary-foreground",
  "secondary", "secondary-foreground", "muted", "muted-foreground", "accent", "accent-foreground",
  "destructive", "destructive-foreground", "success", "success-foreground",
  "warning", "warning-foreground", "info", "info-foreground", "border", "input", "ring",
];
const colorVariables = colorOrder.map((name) => "--" + name);
for (const name of Object.keys(light)) {
  if (!colorVariables.includes(name) && name !== "--radius" && !name.startsWith("--shadow-")) {
    colorVariables.push(name);
  }
}
const shadowVariables = Object.keys(light).filter((name) => name.startsWith("--shadow-"));

function code(text) {
  return backtick + text + backtick;
}

function markdownTable(headers, rows) {
  const head = "| " + headers.join(" | ") + " |";
  const divider = "| " + headers.map(() => "---").join(" | ") + " |";
  return [head, divider].concat(rows.map((row) => "| " + row.join(" | ") + " |")).join("\n");
}

function colorTable(headers) {
  const rows = colorVariables.map((variable) => [
    code(variable),
    classByVariable.has(variable) ? code(classByVariable.get(variable)) : "-",
    code("hsl(" + light[variable] + ")"),
    code("hsl(" + (dark[variable] ?? light[variable]) + ")"),
  ]);
  return markdownTable(headers, rows);
}

function spacingTable(headers) {
  const scale = preset.theme.extend.spacing;
  const keys = Object.keys(scale).sort((left, right) => {
    const toNumber = (key) => (key === "px" ? 0.25 : parseFloat(key));
    const difference = toNumber(left) - toNumber(right);
    return difference !== 0 ? difference : String(left).localeCompare(String(right));
  });
  const rows = keys.map((key) => [code(key), code(scale[key])]);
  return markdownTable(headers, rows);
}

const radius = light["--radius"];
function resolveRadius(value) {
  if (value === "var(--radius)") return radius;
  const calc = String(value).match(/^calc\(var\(--radius\)\s*([+-])\s*(\d+)px\)$/);
  if (calc) {
    const offset = (calc[1] === "-" ? -1 : 1) * Number(calc[2]);
    return String(parseInt(radius, 10) + offset) + "px";
  }
  return String(value);
}

function radiusTable(headers) {
  const rows = Object.entries(preset.theme.extend.borderRadius).map(([key, value]) => [
    code(key), code(String(value)), code(resolveRadius(value)),
  ]);
  return markdownTable(headers, rows);
}

function elevationTable(headers, roles) {
  const rows = shadowVariables.map((variable) => [
    code("shadow-" + variable.slice("--shadow-".length)),
    code(light[variable]),
    code(dark[variable] ?? light[variable]),
    roles[variable] ?? "",
  ]);
  return markdownTable(headers, rows);
}

function durationTable(headers, roles) {
  const rows = Object.entries(preset.theme.extend.transitionDuration).map(([key, value]) => [
    code("duration-" + key), code(String(value)), roles[key] ?? "",
  ]);
  return markdownTable(headers, rows);
}

function easingTable(headers, roles) {
  const rows = Object.entries(preset.theme.extend.transitionTimingFunction).map(([key, value]) => [
    code("ease-" + key), code(String(value)), roles[key] ?? "",
  ]);
  return markdownTable(headers, rows);
}

function render(pack) {
  return [
    ...pack.banner, "",
    pack.title, "",
    ...pack.intro, "",
    pack.colors.heading, "", ...pack.colors.intro, "", colorTable(pack.colors.headers), "",
    pack.spacing.heading, "", ...pack.spacing.intro, "", spacingTable(pack.spacing.headers), "",
    pack.radius.heading, "", ...pack.radius.intro, "", radiusTable(pack.radius.headers), "",
    pack.elevation.heading, "", ...pack.elevation.intro, "", elevationTable(pack.elevation.headers, pack.elevation.roles), "",
    pack.motion.heading, "", ...pack.motion.intro, "", durationTable(pack.motion.durationHeaders, pack.motion.durationRoles), "",
    easingTable(pack.motion.easingHeaders, pack.motion.easingRoles), "",
    pack.conventions.heading, "", ...pack.conventions.bullets, "",
  ].join("\n");
}

const english = {
  banner: [
    "<!-- Generated by scripts/style/tokens.mjs from packages/ui/tailwind.preset.js and",
    "packages/ui/src/index.css. Run pnpm gen:tokens to regenerate; do not edit by hand. -->",
  ],
  title: "# Design tokens",
  intro: [
    "The shared UI uses a three-layer token system: raw scales live in the Tailwind preset",
    "(packages/ui/tailwind.preset.js), semantic colors and elevation live as CSS variables in",
    "packages/ui/src/index.css, and components consume both through Tailwind utilities and cva",
    "variants. This page is generated from those sources; pnpm check:tokens fails when this",
    "page drifts from them.",
  ],
  colors: {
    heading: "## Colors",
    intro: [
      "Semantic colors are HSL triplets consumed as hsl(var(--token)), so the .dark class swaps",
      "values without touching utilities. State colors pair a base with a -foreground variant for",
      "content on solid fills: success, warning and info join destructive for positive, cautionary,",
      "informational and failure states.",
    ],
    headers: ["CSS variable", "Tailwind", "Light", "Dark"],
  },
  spacing: {
    heading: "## Spacing",
    intro: [
      "One 4px scale covers spacing, sizing and translation; components pick keys from this table",
      "(half steps included) instead of inventing values.",
    ],
    headers: ["Key", "Value"],
  },
  radius: {
    heading: "## Radius",
    intro: ["--radius is 8px and the preset derives the rest; larger elements round more."],
    headers: ["Key", "Value", "Resolved"],
  },
  elevation: {
    heading: "## Elevation",
    intro: [
      "Elevation carries structure instead of decorative borders: wells (e0), resting surfaces",
      "(e1-e2), hover lift (e3) and floating layers (e4). Each utility maps to a full box-shadow",
      "list that flips with the theme.",
    ],
    headers: ["Utility", "Light", "Dark", "Role"],
    roles: {
      "--shadow-e0": "Wells: inputs, progress tracks, checkbox fields",
      "--shadow-e1": "Resting surfaces: cards, sidebar, panels",
      "--shadow-e2": "Interactive controls at rest",
      "--shadow-e3": "Hover lift state",
      "--shadow-e4": "Floating layers: dialogs, popovers, toasts",
      "--shadow-focus-ring": "Keyboard focus halo, composed with an elevation",
      "--shadow-sidebar": "Sidebar right edge: hairline plus soft shadow",
    },
  },
  motion: {
    heading: "## Motion",
    intro: [
      "Compose durations and easings, for example duration-motion-normal ease-decelerate for an",
      "entrance. Exits accelerate, hover and press feedback stay standard, and the sidebar keeps",
      "its dedicated rhythm.",
    ],
    durationHeaders: ["Utility", "Value", "Role"],
    durationRoles: {
      "motion-fast": "Presses, color and focus changes",
      "motion-normal": "Standard transitions and entrances",
      "motion-slow": "Large surfaces: dialogs, page transitions",
      sidebar: "Sidebar collapse (kept legacy rhythm)",
    },
    easingHeaders: ["Utility", "Curve", "Role"],
    easingRoles: {
      standard: "General feedback",
      decelerate: "Enter: fast start, soft landing",
      accelerate: "Exit: soft start, quick finish",
      sidebar: "Sidebar collapse curve",
    },
  },
  conventions: {
    heading: "## Usage conventions",
    bullets: [
      "- Pixel-only: spacing, typography and geometry speak px; never em or rem.",
      "- Components use semantic utilities such as bg-success, text-warning and border-info; raw",
      "  Tailwind palette classes are a guardrail violation.",
      "- Combine elevation with the focus halo through an arbitrary value listing the variables,",
      "  for example shadow-[var(--shadow-e2),var(--shadow-focus-ring)].",
      "- Never use transition-all; transition only transform, opacity, box-shadow, background-color",
      "  or color, and never change layout properties on hover, focus or active states.",
      "- Dark mode is class-based: tokens flip automatically, so components never hardcode dark",
      "  palette colors.",
      "- To change a token, edit packages/ui/src/index.css or packages/ui/tailwind.preset.js, run",
      "  pnpm gen:tokens and commit the regenerated pages; CI enforces pnpm check:tokens.",
    ],
  },
};

const chinese = {
  banner: [
    "<!-- 本页由 scripts/style/tokens.mjs 从 packages/ui/tailwind.preset.js 与",
    "packages/ui/src/index.css 生成；重新生成请运行 pnpm gen:tokens，请勿手改。 -->",
  ],
  title: "# 设计令牌",
  intro: [
    "共享 UI 采用三层令牌体系：原始刻度位于 Tailwind preset（packages/ui/tailwind.preset.js），",
    "语义色与海拔位于 packages/ui/src/index.css 的 CSS 变量，组件通过 Tailwind 工具类与 cva",
    "variant 消费两者。本页由上述来源生成；当本页与源不一致时，pnpm check:tokens 会失败。",
  ],
  colors: {
    heading: "## 颜色",
    intro: [
      "语义色是 HSL 三元组，以 hsl(var(--token)) 消费；.dark 类直接更换取值，工具类保持不变。",
      "状态色成对使用：基础色搭配 -foreground 变体，用于实色底上的前景内容。success、warning、",
      "info 与 destructive 分别表达成功、警示、提示与失败/危险状态。",
    ],
    headers: ["CSS 变量", "Tailwind", "亮色", "暗色"],
  },
  spacing: {
    heading: "## 间距",
    intro: ["统一的 4px 刻度覆盖间距、尺寸与位移；组件从该表取键（含半阶），不得自造数值。"],
    headers: ["键", "值"],
  },
  radius: {
    heading: "## 圆角",
    intro: ["--radius 为 8px，其余由 preset 派生；元素越大，圆角越大。"],
    headers: ["键", "值", "解析值"],
  },
  elevation: {
    heading: "## 海拔",
    intro: [
      "海拔取代装饰性描边承担结构：凹井（e0）、静置面（e1-e2）、hover 抬升（e3）与浮层（e4）。",
      "每个工具类映射到一条随主题切换的完整 box-shadow 阴影列表。",
    ],
    headers: ["工具类", "亮色", "暗色", "用途"],
    roles: {
      "--shadow-e0": "凹井：输入件、进度轨道、勾选框底",
      "--shadow-e1": "静置面：卡片、侧栏、面板",
      "--shadow-e2": "交互控件静置态",
      "--shadow-e3": "hover 抬升态",
      "--shadow-e4": "浮层：对话框、弹出层、toast",
      "--shadow-focus-ring": "键盘焦点光环，可与海拔叠加",
      "--shadow-sidebar": "侧栏右缘：发丝线加柔影",
    },
  },
  motion: {
    heading: "## 动效",
    intro: [
      "时长与缓动按标尺组合：进入用 duration-motion-normal ease-decelerate，退出用 accelerate，",
      "hover 与按压反馈用 standard，侧栏保留专属节奏。",
    ],
    durationHeaders: ["工具类", "值", "用途"],
    durationRoles: {
      "motion-fast": "按压、色彩与焦点变化",
      "motion-normal": "标准过渡与进入",
      "motion-slow": "大件表面：对话框、页面转场",
      sidebar: "侧栏折叠（保留的既有节奏）",
    },
    easingHeaders: ["工具类", "曲线", "用途"],
    easingRoles: {
      standard: "通用反馈",
      decelerate: "进入：快出缓收",
      accelerate: "退出：缓出快收",
      sidebar: "侧栏折叠曲线",
    },
  },
  conventions: {
    heading: "## 用法约定",
    bullets: [
      "- 仅用 px：间距、字号与几何一律使用 px，不使用 em/rem。",
      "- 组件只用语义工具类（如 bg-success、text-warning、border-info）；原始 Tailwind 色板类",
      "  属于样式护栏违规。",
      "- 需要叠加海拔与焦点光环时，使用列出变量的任意值，例如",
      "  shadow-[var(--shadow-e2),var(--shadow-focus-ring)]。",
      "- 禁用 transition-all；只过渡 transform、opacity、box-shadow、background-color 或 color；",
      "  hover/focus/active 状态不得改变布局属性。",
      "- 暗色模式基于 .dark 类：令牌自动翻转，组件不得硬编码暗色色板。",
      "- 修改令牌时编辑 packages/ui/src/index.css 或 packages/ui/tailwind.preset.js，运行",
      "  pnpm gen:tokens 并提交再生成页面；CI 通过 pnpm check:tokens 强制同步。",
    ],
  },
};

const targets = [
  { path: join(root, "docs", "development", "design-tokens.md"), content: render(english) },
  { path: join(root, "docs", "zh", "development", "design-tokens.md"), content: render(chinese) },
];

if (mode === "--write") {
  for (const target of targets) {
    writeFileSync(target.path, target.content);
    console.log("Updated " + relative(root, target.path) + " from the live token sources.");
  }
} else {
  const stale = [];
  for (const target of targets) {
    const name = relative(root, target.path);
    let committed = "";
    try {
      committed = readFileSync(target.path, "utf8").replaceAll("\r\n", "\n");
    } catch {
      committed = "";
    }
    if (committed !== target.content) stale.push(name);
    else console.log(name + " matches the token sources.");
  }
  if (stale.length > 0) {
    throw new Error(stale.join(", ") + " is stale. Run pnpm gen:tokens and review the diff.");
  }
}
