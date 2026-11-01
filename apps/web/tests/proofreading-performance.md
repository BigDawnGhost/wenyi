# Large-chapter rendering measurement / 大章节渲染测量

Run from the repository root with the normal frontend dependencies installed:

```sh
pnpm -C apps/web test:e2e --project=performance --no-deps --repeat-each=3

# Same shared behavior through the Desktop entry
pnpm -C apps/desktop/frontend test:e2e --project=desktop-performance --no-deps --repeat-each=3

# Optional stress size (supported range: 1000–5000)
PERF_PARAGRAPHS=5000 pnpm -C apps/web test:e2e --project=performance --no-deps
```

The normal `test:e2e` run executes the functional Web tests first, then both
large-chapter tests in the one-worker `performance` project. `--no-deps` runs only
the measurements locally. This prevents concurrent browser workloads from
distorting measurements on small CI runners. Initial visibility uses the existing
30-second whole-test budget instead of treating the default five-second assertion
timeout as a first-render SLA. The complete cold startup remains in `firstRenderMs`;
render-count assertions and the whole-test deadline are unchanged.
Use Playwright's installed Chromium; set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` only when
an explicit system-browser comparison is needed. Tracing adds substantial overhead
to the large DOM fixture and should be disabled for comparable measurements.

常规 `test:e2e` 先执行 Web 功能测试，再由单 worker 的 `performance`
项目执行两个大章节测试。局部测量可使用 `--no-deps`，避免同时运行的浏览器负载
影响小型 CI 机器上的结果。首屏就绪等待使用已有的整条测试 30 秒预算，不把默认
5 秒断言超时当作首屏性能门槛；完整冷启动仍计入 `firstRenderMs`，渲染次数断言
与整条测试截止时间保持不变。默认使用 Playwright 安装的
Chromium，仅在明确比较系统浏览器时设置 `PLAYWRIGHT_CHROMIUM_EXECUTABLE`。
Trace 对大 DOM 用例有显著开销，可比较的性能测量应关闭 Trace。

The fixture generates long English/Chinese paragraphs and mocks HTTP and WebSocket
traffic. It does not read books, start a backend, or call model services.
Each measurement is printed as JSON and attached to the Playwright result.

## Earlier per-row memoization baseline / 先前的逐行 memo 优化基线

Headless system Chromium, Vite development server, 1440 × 900 viewport, 2000
paragraphs, three serial runs per version:

| Measurement | Before | After |
| --- | --- | --- |
| Navigation → first visible row and all paragraph nodes present | 2376–2506 ms | 1728–1814 ms |
| Open edit action → textarea value visible | 799–850 ms | 259–304 ms |
| Fill draft → value assertion | 63–71 ms | 62–80 ms |
| Continuous scrolling rAF p95 | 16.7–16.8 ms | 16.7–16.8 ms |
| Continuous scrolling maximum rAF interval | 16.8 ms | 16.8–33.3 ms |
| Long tasks, representative run, total measured workflow | 5 / 1965 ms | 4 / 1218 ms |

The bottleneck reproduced here is entering editing and initial rendering, **not**
a sustained scrolling frame-rate regression. Opening one editor previously
reconciled the entire inline paragraph list with fresh per-row callbacks.
Rows now have primitive props and a stable opener behind `memo`; unchanged
server refreshes do not render paragraph actions. `content-visibility: auto`
skips offscreen layout/paint without removing paragraphs from the DOM.

The development React hook counts committed `ParagraphActions` work; it remembers
fiber render timestamps so stale flags on reused subtrees are not counted again.
An unchanged active-task poll renders zero actions and opening the editor
renders one (resetting that row's expanded state), enforced by assertions rather than unstable
wall-clock thresholds. The original exploratory counter did not deduplicate stale
flags, so its polling counts are deliberately not presented as baseline evidence.

The second test verifies far-paragraph deep-link focus, Chromium native text find,
cross-paragraph selection, keyboard menu access, conflict refreshes while a draft
is open, and a saved empty translation remaining editable. Existing proofreading
and paragraph-editor tests additionally cover copy, save, visible progress, long
textarea sizing and desktop editor presentation.

## Shared-menu and idle-query policy / 共享菜单与空闲查询策略

Paragraph actions no longer allocate menu state, refs or effects per row. A chapter
mounts at most one menu, and collapsed review notes do not mount their evidence until
first expansion. Full paragraph DOM is retained. The measurement first observes
unchanged polling during translation, then pauses the fixture before editing.
Idle project pages and revision history do not poll; saves and conflicts still
refresh current values. Separate project-polling tests check completed/paused pages,
final snapshots (including in-flight reads), and resuming an idle task.

段落不再各自分配菜单状态、ref 和 Effect，每章最多挂载一个操作菜单。
折叠的章节审校记录在首次展开前不创建内容节点，完整段落 DOM 仍然保留。
测量先验证翻译期间的无变化轮询，再暂停后编辑；空闲项目页面及改动记录停止轮询，
保存与冲突仍会刷新当前值。独立用例覆盖完成／暂停后的零轮询、进行中请求与最终
快照的竞态，以及恢复任务后重新轮询。Desktop 也有串行性能测试项目，避免与功能
测试竞争浏览器资源。上面的历史耗时不是这次共享菜单改动的前后比较。

### Local production-build comparison / 本地生产构建对比

A separate offline check compared Desktop production assets at `b7bf4c9` with
this change, alternating before/after in fresh Chromium contexts, three runs per
case, at 1440 × 900 without CPU throttling. It used the same generated paragraph
text as the test, also returning `target_before_polish` for each paragraph.
Review notes contained six short fields each. Initial readiness was recorded
after all paragraph nodes were inserted and two animation frames elapsed.
The following timings are medians, not CI budgets or native WebView claims:

另用离线脚本对比 `b7bf4c9` 与本次修改的 Desktop 生产产物。每组在新的 Chromium
上下文中交替测量前后版本，各运行三次；窗口 1440 × 900，无 CPU 限速。段落文本
与测试相同，响应额外包含每段的润色前译文；每条审校记录包含六个简短字段。
首屏指标在全部段落节点插入并经过两帧后记录。以下耗时为中位数，不是 CI 门槛，
也不代表原生 WebView 性能：

| Scenario / 场景 | Before / 修改前 | After / 修改后 |
| --- | --- | --- |
| 5,000 paragraphs, initial readiness / 5,000 段首次就绪 | 678 ms | 651 ms |
| 5,000 paragraphs, longest initial task / 首次最长主线程任务 | 296 ms | 274 ms |
| 1,000 paragraphs + 1,000 collapsed notes, initial readiness / 首次就绪 | 289 ms | 241 ms |
| Same case, DOM elements / 同场景 DOM 元素 | 38,179 | 12,178 |
| Same case, edit action to textarea / 编辑操作到输入框 | 151 ms | 64 ms |
| Idle chapter reads over 6.3 s / 空闲 6.3 秒内整章读取次数 | 2 | 0 |

Idle reads were sampled once per case. Editing times include Playwright IPC and
actionability checks. For 5,000 paragraphs without notes, editor timings remained
similar (235 ms versus 228 ms); this change does not eliminate full-DOM scaling or
claim a large improvement for every chapter.

空闲请求数每组采样一次；编辑耗时包含 Playwright IPC 与可操作性检查。
没有审校记录的 5,000 段场景，打开编辑耗时接近（235 ms 对 228 ms）。
本次没有消除完整 DOM 随段落数增长的成本，也不宣称所有章节都有大幅改善。

### Initial native Tauri / WebKitGTK verification / 原生窗口首次验证

Before the menu/textarea follow-up below, the actual Linux/Wayland Tauri window was tested:
WebKitGTK 2.54.2, GTK 3.24.52, 1280 × 850 CSS pixels, device pixel ratio 2,
production frontend assets embedded in a debug Rust host. Build commands were
`pnpm -C apps/desktop/frontend build` and
`cargo build --locked --manifest-path apps/desktop/Cargo.toml`.
The host ran with an explicit temporary `--data-dir`, temporary XDG data/cache/config
directories, and the workspace Python interpreter. The real Python HTTP backend
and SQLite adapters served synthetic mixed-language chapters; no HTTP mocks, real
books, or model calls were used.

A temporary harness drove the host's own WebView through WebKit's loopback-only
remote inspector, not Chromium or a standalone WebKit browser. Three serial runs
restarted the host; each loaded the 1,000-paragraph, 5,000-paragraph, then
1,000-paragraph/1,000-note case. Readiness measures route entry through all row
nodes being present plus two animation frames, excluding application launch.
Menu, editor and draft measurements use in-page clocks through two animation
frames after each action, excluding inspector transport time. These initial
observations precede the follow-up below and are **not a native before/after comparison**:

下节菜单／输入框修复之前，先在实际 Linux/Wayland Tauri 窗口中测试：系统 WebKitGTK 2.54.2、
GTK 3.24.52，窗口 1280 × 850 CSS 像素、像素比 2，debug Rust 宿主加载生产前端产物。
使用显式临时 `--data-dir`、独立 XDG 数据／缓存／配置目录及工作区 Python 解释器；
合成中英文章节由真实 Python HTTP 后端与 SQLite 提供，不使用 Fake API、实际书籍或模型服务。
临时脚本通过仅监听本机回环地址的 WebKit Inspector 操作宿主自己的 WebView，
不是 Chromium 或独立 WebKit 浏览器。串行运行三次，每次重启宿主，依次测量三组数据。
首屏指标包含路由进入到全部段落节点插入并经过两帧的时间，不包含应用启动；
菜单、编辑器及草稿耗时使用页面内计时并等待两帧，不包含 Inspector 通信时间。
以下为**后续菜单／输入框修复之前的原生实测，不是原生前后对比**：

| Measurement / 指标 | 1,000 paragraphs / 段 | 5,000 paragraphs / 段 | 1,000 paragraphs + 1,000 folded notes / 段及折叠记录 |
| --- | --- | --- | --- |
| Readiness, median (range) / 首屏中位数（范围） | 505 ms (503–518) | 1548 ms (1524–1586) | 341 ms (341–482) |
| Longest initial rAF gap, median / 首屏最长帧间隔中位数 | 220 ms | 1184 ms | 246 ms |
| Open menu, median / 打开菜单中位数 | 210 ms | 884 ms | 201 ms |
| Edit action → textarea, median / 编辑操作到输入框中位数 | 228 ms | 1153 ms | 225 ms |
| Update draft, median / 更新草稿中位数 | 29 ms | 89 ms | 33 ms |
| Scrolling rAF p95, run range / 滚动帧间隔 p95 范围 | 18–19 ms | 38–40 ms | 18–19 ms |
| DOM elements before expanding notes / 展开记录前 DOM 元素 | 12,178 | 60,178 | 12,178 |

The folded notes were absent from the DOM until expansion. Expanding all 1,000
notes grew the DOM to 42,180 elements and took 513–534 ms. Each editor opening
raised `ResizeObserver loop completed with undelivered notifications.`; the
warning was recorded, not treated as an error-free pass. No other unhandled page
errors were observed. **At that stage the 5,000-paragraph native loading/menu/editor
stalls remained unresolved**, despite the idle-query and lazy-notes improvements.

Functional checks passed for far-paragraph focus, WebKit `window.find`,
cross-paragraph selection, keyboard-event menu access, persistence to SQLite,
and a real HTTP 409 retaining the draft while refreshing a saved empty translation.
Completed and paused chapters with revision history open, both glossary states,
the final active-to-paused snapshot, and the idle dashboard each generated zero
project polling requests in a settled 6.5-second observation. An active project
generated two metadata and two chapter reads over 6.5 seconds. Active/terminal
statuses and the final batch were written to the isolated store without running
a translation job; task execution and checkpointing were not exercised.

折叠的 1,000 条记录在首次展开前没有内容节点；展开后 DOM 增至 42,180 个元素，
耗时 513–534 ms。每次打开编辑器均出现
`ResizeObserver loop completed with undelivered notifications.`，已记录警告，
不算无错误通过；未观察到其他未处理页面错误。**当时 5,000 段场景在原生 WebKit 中的
加载、菜单和编辑器卡顿仍未解决**，不能用停止轮询、延迟展开的收益掩盖这些问题。
功能上已验证远处段落聚焦、WebKit 查找、跨段选择、键盘事件菜单、实际 SQLite 保存，
以及真实 HTTP 409 后保留草稿并刷新空译文。完成／暂停状态下的章节及改动记录、
两种状态的术语页、运行转暂停后的最终快照、空闲项目列表，分别稳定观察 6.5 秒，
项目轮询请求均为零；运行状态的 6.5 秒内有两次项目元数据和两次整章读取。
任务状态与最后一批译文由测试写入隔离存储，没有启动翻译任务，不覆盖任务执行及检查点。

Native scrolling used the same 120 rAF steps of 60 px, not physical wheel inertia.
DOM actions and keyboard events were inspector-driven, not OS input or IME tests.
This WebKit build does not expose the `longtask` observer, so initial rAF gaps
are reported separately and must not be equated with Chromium long-task metrics.
There was no packaged-release, macOS WKWebView, or Windows WebView2 run.
All owned test processes were stopped; user workspaces and global graphics settings
were left unchanged.

原生滚动仍使用 120 帧、每帧 60 px 的脚本，不代表实体滚轮惯性；DOM 操作及键盘事件
由 Inspector 驱动，不覆盖系统输入或输入法。该 WebKit 不支持 `longtask` 观察器，
因此首屏帧间隔不能与 Chromium 长任务指标等同。本次未测发布安装包、macOS WKWebView
或 Windows WebView2。测试进程已停止，没有改动用户工作区或全局图形设置。

### Native menu and textarea follow-up / 原生菜单与输入框修复

Isolated probes found that adding the menu's global `pointerdown` listener, not
React's row updates, accounted for most of the menu delay. A passive pointer
listener did not remove it. Outside dismissal now uses capturing `click` and
`contextmenu` events; scrolling, keyboard navigation and focus restoration remain.
This changes dismissal from pointer press to activation without introducing a
WebKit-only branch or listeners per paragraph.

The dialog now opens in a layout effect before measuring the textarea. Draft
changes resize synchronously; a separate observer reads its delivered width and
coalesces width-driven resizing into a cancellable animation frame. The first
delivery is included to handle initial width changes, but typing does not recreate
the observer. No synchronous height writes occur inside observer delivery.

定位实验表明，菜单的大部分延迟来自新增全局 `pointerdown` 监听，而非段落 React
更新；仅改成 passive 不能消除延迟。外部关闭改用捕获阶段的 `click`、`contextmenu`，
保留滚动关闭、键盘导航和焦点恢复，不增加 WebKit 专用分支或逐段监听器。
关闭时机从指针按下改为激活。编辑器先在布局 Effect 中打开弹窗，再测量输入框；
草稿变化立即调整高度，独立观察器使用通知中的宽度，把宽度变化后的调整合并到可取消的
下一帧。首次通知也会处理，防止初始宽度变化遗漏；输入文字不再重建观察器，
观察器通知期间不再同步写入高度。

Final verification alternated the saved pre-fix native binary and the rebuilt
binary, restarting the host for each run, three runs per version. Each run loaded
1,000 then 5,000 paragraphs under the same native environment and synthetic-data
method above, without diagnostic overrides or profiling instrumentation:

最终验证交替运行保留的修复前原生二进制和重新构建的版本，各三次，每次重启宿主，
依次加载 1,000、5,000 段；环境及合成数据方法同上，不启用诊断覆盖或 profiler：

| Measurement / 指标 | Before / 修复前 | After / 修复后 |
| --- | --- | --- |
| 1,000 paragraphs, menu median (range) / 菜单中位数（范围） | 185 ms (181–193) | 36 ms (33–39) |
| 5,000 paragraphs, menu median (range) / 菜单中位数（范围） | 877 ms (854–886) | 49 ms (36–52) |
| 5,000 paragraphs, editor median (range) / 编辑器中位数（范围） | 1152 ms (1136–1222) | 1137 ms (1046–1159) |
| ResizeObserver warnings across six editor openings / 六次打开编辑器的警告数 | 6 | 0 |

The menu improvement is repeatable; **editor opening remains about one second**.
A separate probe opened a plain native `<dialog>` directly with JavaScript over
the same chapter, bypassing the React editor and omitting backdrop blur. Readiness
still took about one second; the existing page remained React-based. This does not
identify the costly WebKit subsystem or mean the `showModal()` call itself blocked
for a second. Full-DOM startup and scrolling remain bottlenecks; this follow-up
does not claim improvements there or replace native modal focus handling with a
custom focus trap.

Two added Playwright regressions failed before the fix (global pointer registration
and ResizeObserver errors) and passed afterward on both frontend entries. Native
checks additionally passed outside click/context-menu dismissal, keyboard focus,
long-draft fitting at three actual textarea widths, shrinking, tab changes during
resizing, real SQLite save, real HTTP 409 with draft preservation, a paused
6.5-second zero-polling observation, deep links, find and selection, without page
errors. Native width checks changed the dialog's width, not the OS window. At the
time of this measurement, Playwright separately covered viewport sizes 900, 390
and 1280 pixels. Current editor regression tests cover 1024, 1280 and 1440 pixels;
the desktop-only suite separately checks narrow desktop windows and mobile UAs.

菜单收益可重复，**编辑器打开仍约一秒，不算解决**。独立实验通过原生 JavaScript
在同一章节上打开简单的原生 `<dialog>`，绕过 React 编辑器且不使用背景模糊，界面就绪
仍需约一秒；原来的页面仍基于 React。这没有定位到 WebKit 内部具体的耗时子系统，
也不表示 `showModal()` 调用本身同步阻塞了一秒。完整 DOM 的首次展示与滚动仍是瓶颈；
本次不宣称这两项改善，也没有把原生模态焦点约束换成自制焦点陷阱。
新增两条 Playwright 回归先在旧代码失败（全局指针监听、ResizeObserver 错误），
修复后在两端通过。原生另验证外部点击／右键关闭、键盘焦点、三种输入框实际宽度下的
长草稿撑高、缩短回收、改宽时切换标签、真实 SQLite 保存、真实 HTTP 409 保留草稿、
暂停后 6.5 秒零轮询、深链接、查找和选区，未出现页面错误。原生宽度检查调整的是弹窗
而非系统窗口；当时 Playwright 另覆盖 900、390、1280 像素视口。当前编辑器回归覆盖
1024、1280、1440 像素；电脑端入口测试另行检查电脑窄窗口和移动设备 UA。

## Limits / 限制

The Playwright measurements use development builds; the separately labeled
Chromium production comparison uses built assets. Both are local Chromium observations,
**not system WebView / WebKit performance claims**. There is no CPU throttling; timings include
Playwright actionability/IPC and initial module loading. Long-task totals span
the whole workflow, not just first paint. rAF scrolling uses fixed 60 px steps for
120 frames, not OS wheel inertia. React hook instrumentation adds overhead and
depends on development Fiber internals. Browser-native find is exercised through
Chromium's `window.find`, not a GUI find toolbar. Full DOM memory remains linear
in paragraph count; this intentionally avoids virtualization's selection/find
and keyboard-access tradeoffs.

以上命令完全离线，使用临时合成中英文混排章节和 Fake API，不接触真实书籍。
先前逐行 memo 优化的实测改善主要是首次展示和打开段落编辑：首次展示约从 2.4 秒降到
1.7–1.8 秒，打开编辑从约 0.8 秒降到 0.26–0.30 秒。输入和连续滚动
没有可证实的改善，不能以这些 Chromium 数据推断 Desktop WebKit 已修复。
完整段落 DOM 保留，未机械虚拟化；测试覆盖浏览器查找、深链接、跨段选择、
键盘菜单、更新期间草稿保留及空译文。当前空闲轮询策略见上节。
原生 Tauri 验证单独记录于上节，其剩余卡顿与测量边界不由 Chromium 结果覆盖。
