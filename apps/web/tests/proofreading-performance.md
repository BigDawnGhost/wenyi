# Large-chapter rendering measurement / 大章节渲染测量

Run from the repository root with the normal frontend dependencies installed:

```sh
pnpm -C apps/web test:e2e --project=performance --no-deps --repeat-each=3

# Optional stress size (supported range: 1000–5000)
PERF_PARAGRAPHS=5000 pnpm -C apps/web test:e2e --project=performance --no-deps
```

The normal `test:e2e` run executes the 125 functional Web tests first, then both
large-chapter tests in the one-worker `performance` project. `--no-deps` runs only
the measurements locally. This prevents concurrent browser workloads from
distorting measurements on small CI runners. Initial visibility uses the existing
30-second whole-test budget instead of treating the default five-second assertion
timeout as a first-render SLA. The complete cold startup remains in `firstRenderMs`;
render-count assertions and the whole-test deadline are unchanged.
Use Playwright's installed Chromium; set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` only when
an explicit system-browser comparison is needed. Tracing adds substantial overhead
to the large DOM fixture and should be disabled for comparable measurements.

常规 `test:e2e` 先执行 125 个 Web 功能测试，再由单 worker 的 `performance`
项目执行两个大章节测试。局部测量可使用 `--no-deps`，避免同时运行的浏览器负载
影响小型 CI 机器上的结果。首屏就绪等待使用已有的整条测试 30 秒预算，不把默认
5 秒断言超时当作首屏性能门槛；完整冷启动仍计入 `firstRenderMs`，渲染次数断言
与整条测试截止时间保持不变。默认使用 Playwright 安装的
Chromium，仅在明确比较系统浏览器时设置 `PLAYWRIGHT_CHROMIUM_EXECUTABLE`。
Trace 对大 DOM 用例有显著开销，可比较的性能测量应关闭 Trace。

The fixture generates long English/Chinese paragraphs and mocks HTTP and WebSocket
traffic. It does not read books, start a backend, or call model services.
Each measurement is printed as JSON and attached to the Playwright result.

## Evidence

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
After the change an unchanged poll renders zero actions and opening the editor
renders one (closing that row's menu), enforced by assertions rather than unstable
wall-clock thresholds. The original exploratory counter did not deduplicate stale
flags, so its polling counts are deliberately not presented as baseline evidence.

The second test verifies far-paragraph deep-link focus, Chromium native text find,
cross-paragraph selection, keyboard menu access, background updates while a draft
is open, and a saved empty translation remaining editable. Existing proofreading
and paragraph-editor tests additionally cover copy, save, visible progress, long
textarea sizing and mobile presentation.

## Limits / 限制

These are local Chromium development-build observations, **not system WebView /
WebKit performance claims**. There is no CPU throttling; timings include
Playwright actionability/IPC and initial module loading. Long-task totals span
the whole workflow, not just first paint. rAF scrolling uses fixed 60 px steps for
120 frames, not OS wheel inertia. React hook instrumentation adds overhead and
depends on development Fiber internals. Browser-native find is exercised through
Chromium's `window.find`, not a GUI find toolbar. Full DOM memory remains linear
in paragraph count; this intentionally avoids virtualization's selection/find
and keyboard-access tradeoffs.

以上命令完全离线，使用临时合成中英文混排章节和 Fake API，不接触真实书籍。
本次实测改善主要是首次展示和打开段落编辑：首次展示约从 2.4 秒降到
1.7–1.8 秒，打开编辑从约 0.8 秒降到 0.26–0.30 秒。输入和连续滚动
没有可证实的改善，不能以这些 Chromium 数据推断 Desktop WebKit 已修复。
完整段落 DOM 保留，未机械虚拟化；测试覆盖浏览器查找、深链接、跨段选择、
键盘菜单、轮询期间草稿保留及空译文。轮询和 WebSocket 一致性策略未变。
