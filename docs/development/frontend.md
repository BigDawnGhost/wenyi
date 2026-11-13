# Frontend development

Web and Desktop share pages and primitives, not platform implementations.
This page defines the routing, loading, accessibility and styling rules for both
hosts. Host setup is covered by [Web development](web.md) and
[Desktop development](desktop.md).

## Shared code and host boundaries

Shared features live in `packages/ui`; Web owns `apps/web/src`, and Desktop owns
`apps/desktop/frontend/src`. Both hosts configure the
[platform services contract](../../packages/ui/src/platform/contracts.ts#L122)
before rendering shared pages.

- Shared UI must not import either app, inspect native globals or guess a platform
  from `isDesktop`-style flags.
- Transport, progress subscriptions, preferences, file operations and optional
  native capabilities come through injected services. Do not call `fetch`,
  `new WebSocket`, `localStorage` or `sessionStorage` directly from shared UI.
- Web output must contain no native bootstrap, IPC or vault UI. Desktop includes
  its own adapters and does not depend on a Web deployment.
- Expose Desktop-only panels through optional capabilities, not shared implementations.
  An unavailable capability must not leave a broken control in Web.
- Keep API changes aligned with backend schemas, shared generated types and both
  adapters. For a contract change, regenerate and check the schema with
  `pnpm gen:schema` and `pnpm check:schema`.

The [platform boundary checks](../../packages/ui/tests/platform-boundaries.test.mjs#L17)
inspect both source and built output. Source-only checks cannot establish that the
Web bundle is free of native code.

## Routes and persistent shell

The [route manifest](../../packages/ui/src/routes/manifest.tsx#L27) is the shared
source of truth. It keeps `routeEntries`, `pageLoaders`, navigation entries and
settings-section availability in one module. Register new feature pages, their
lazy loader and any navigation entry there; do not create parallel route tables,
warm-up lists or sidebar lists in the hosts.

The application renders URL routes from the manifest and warms page chunks at
startup. Navigation prefetches the same loader on pointer entry and keyboard focus.
Retain URL routing and the wildcard redirect; route aliases can share one loader.
Settings sections use capability availability rather than assuming both hosts expose
the same native panels.

The shell remains mounted during navigation. The
[route gate](../../packages/ui/src/routes/RouteGate.tsx#L65) wraps the outlet
**inside** the layout's main content area:

- Its `Suspense` fallback is a quiet, text-free `.route-placeholder` with
  `aria-busy="true"` and `data-route-pending`.
- Its error boundary replaces only route content and resets on a pathname change.
  The fallback keeps a Reload action because React lazy imports cache rejection;
  do not claim that resetting a boundary reloads a failed chunk.
- A root `AppErrorBoundary` handles shell/provider failures. Do not move the route
  gate or a `Suspense` boundary above the shell.

For component-level lazy imports, use the smallest local
[LazyBoundary](../../packages/ui/src/routes/LazyBoundary.tsx#L3), or explicitly warm
the loader. Its default fallback is `null`; a stable local placeholder is also
appropriate. **It wraps Suspense only, not an error boundary.** Do not replace the
surrounding page when an optional widget loads.

[Routing rules](../../packages/ui/tests/routing-rules.test.mjs#L28) restrict direct
`Suspense` to the route/lazy gates, check lazy imports and manifest consistency.
Reuse these gates rather than introducing page-local `Suspense`.

## Loading and error boundaries

Suspense handles code splitting. Query loading is explicit and belongs to the
smallest region waiting for data.

| Level | Situation | Presentation |
| --- | --- | --- |
| Route code | A route chunk is not ready | Quiet route gate; shell/navigation remain visible |
| Initial data | A region has no data yet | Inline loading or empty state in that region |
| Long task | Translation, analysis or export is running | Inline progress or notification using existing events and `role="status"` |
| Failure | A query, action or render fails | Inline error, route boundary or root boundary according to its owning scope |

Apply these query rules:

1. With no data, `isPending` may show inline loading. With data and `isFetching`,
   keep the content and optionally show a small refresh indicator.
2. A background error appears **alongside retained data**. Without data, show the
   error and a retry action in the empty region. The shared
   [ErrorNotice](../../packages/ui/src/components/ui/data.tsx#L5) renders the alert;
   the caller owns any retry control.
3. A first-paint gate is monotonic for the current data identity: after revealing
   content, refresh must not repeatedly hide/remount it.
4. Preserve data only for the identity it belongs to. On a project/workspace/run
   change, never show a previous project's metadata, progress or editable state as
   the newly selected one. Keeping the shell is not permission to mix query identities.

Short code-loading placeholders should not flash “Loading…” text. Initial data
loading can use localized inline copy and `role="status"`. Reuse the existing
`.route-placeholder`/`.runtime-placeholder` animations, including their
`prefers-reduced-motion` behavior. Do not add animation delays to shutdown.

Use inline errors for recoverable operations; never catch a render error and return
a blank page. Page code must not use `location.reload()` as ordinary retry behavior.
Long tasks should not block navigation or replace the whole page.

## Continuous chapter reader

Proofreading uses a virtual continuous scroll surface, not chapter pagination.
The viewport requests 32-paragraph windows and keeps neighboring windows ready;
abandoned requests are cancelled and inactive windows are evicted. Dense reader
positions exclude whitespace-only sources but include headings. Stable segment IDs,
not positions, identify edits and deep links.

The window endpoint accepts at most 256 paragraphs. Translation refresh submits
known paragraph revisions and receives only changed targets and before-polish
values, never source text. Apply a response only if its previous revision still
matches the cached paragraph. Terminal events and Desktop foreground reconciliation
refresh targets, not immutable source windows. Unsaved editor drafts remain separate.
Review issues load only when expanded.

Selection endpoints and an open editor retain their needed paragraphs. Copying
across unloaded rows reads bounded windows rather than mounting the intervening DOM.
Browser find/select-all cannot cover unmounted text; there is no chapter-wide search.
Use the opt-in 65,000-paragraph performance suite in both hosts to check bounded
mounting, random deep links, selection, incremental updates and editor conflicts.
Mocked browser timings do not establish native WebView or database performance.

## Accessibility and semantic slots

Use semantic HTML and accessible names for interactive behavior: labeled navigation,
one appropriate main landmark, meaningful heading levels, named buttons/fields,
`role="status"` for progress and `role="alert"` for failures. A `data-slot` is a stable
structural identifier, not a substitute for accessibility.

Use `data-slot="<region>.<element>"` with kebab-case names for structural regions.
Keep these names stable when changing appearance; migrate them deliberately when
changing structure. The shared [layout](../../packages/ui/src/components/layout/AppLayout.tsx#L140)
and primitives define the core vocabulary:

| Slot | Meaning |
| --- | --- |
| `sidebar` | Entire sidebar |
| `sidebar.logo-row` | Brand/settings heading and collapse control |
| `sidebar.strata` | Sidebar layers beneath the logo row; `#sidebar-navigation` |
| `sidebar.action` | Primary action |
| `sidebar.panels` | Global navigation |
| `sidebar.region` | Scrollable project navigation |
| `sidebar.footer` | Bottom navigation/action area |
| `content` | Main route-content region |
| `page.header` | Page title, subtitle and actions |
| `page.container` | Page body |
| `card`, `card.header`, `card.title`, `card.content` | Card and its parts |
| `button` | Shared button primitive |

Register new shared structural slots in this table. Tests should locate structural
regions by slots and interactions by ARIA role plus accessible name, not utility
classes or incidental DOM nesting. Dedicated markers such as `data-route-pending`
and Desktop `data-runtime-state` describe lifecycle state, not visual appearance.

## Styling

Use inline Tailwind utility classes for static styling. Do not invent utility-like
semantic classes such as `sidebarBox` or `navItemActive`. Prefer `disabled:`,
`aria-*` and `data-*` variants for state, not new state class names.

The [Tailwind preset](../../packages/ui/tailwind.preset.js#L3) defines spacing,
type, line-height, letter-spacing, max-width, radii and translation scales in **px**.
Use px for new fixed CSS lengths and arbitrary utilities rather than rem/em.
Viewport units remain appropriate for viewport constraints; percentages are
appropriate for relative dimensions and dynamic progress/position values.
Unitless ratios are not fixed lengths.

Use inline `style` only for runtime values, such as progress width, computed menu
coordinates or `contentVisibility`. Put static fonts, colors and spacing in classes.

When custom CSS is necessary, use a registered kebab-case name and keep it in the
shared [stylesheet](../../packages/ui/src/index.css#L15):

| Class | Purpose |
| --- | --- |
| `brand-wordmark` | Shared brand typography |
| `runtime-placeholder` | Desktop runtime pending/closing gate |
| `route-placeholder` | Quiet in-content route gate |
| `disclosure-summary`, `disclosure-chevron` | Native disclosure styling |

These are authoring conventions, not a claim that every dependency or older
arbitrary utility is px-only. Keep shared theme tokens and reduced-motion behavior
consistent in both hosts; do not duplicate styles into an app to bypass the shared rule.

## Dialogs and global notifications

Use the shared [Dialog](../../packages/ui/src/components/ui/misc.tsx#L7), a
body-portaled Radix Dialog. Supply an accessible title, optional description,
appropriate initial/return focus and a disabled-close state only when the workflow
requires it. Keep scrolling in the dialog body with actions reachable in short
windows. Radix Select's portaled options must remain usable above the dialog.
Do not replace this with native `showModal()`, whose browser top layer would cover
those options. The shared backdrop uses 2px blur.

Mount [NotificationToaster](../../packages/ui/src/components/NotificationToaster.tsx#L5)
once in the shell, outside scrolling route content. Its body portal is viewport-fixed
at the top right, above dialogs/dropdowns, with usable close controls. Async feedback
does not follow the last clicked button; scrolling or unrelated clicks must not move it.

Use the shared [toast adapter](../../packages/ui/src/lib/toast.ts#L5):
success defaults to one second, errors/warnings to ten seconds and informational
messages to five seconds. Callers can override duration where required. A notification
does not replace actionable inline validation; for example, a form save failure
remains visible in the open form after its toast expires.

## Validation in both hosts

Use Node 22 and pnpm 9. Install dependencies with `pnpm install --frozen-lockfile`
and Playwright Chromium with `pnpm -C apps/web exec playwright install chromium`
when it is not already available. From the repository root:

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

Both builds must precede the boundary check. Run affected E2E specs for scoped changes;
route/shell changes warrant both full suites. The Playwright configs start local Vite
servers on 4173 (Web) and 4174 (Desktop). The Desktop browser suite mocks native IPC;
it does not launch Tauri. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` selects an existing browser,
and `PLAYWRIGHT_WORKERS` controls concurrency. Performance projects are opt-in with
`PLAYWRIGHT_PERF=1`, separate from ordinary behavior checks.

For each affected page, verify:

- Cold/slow navigation retains the same connected shell/sidebar DOM node; loading
  stays in the appropriate region and the wildcard route still works.
- Background refresh retains matching data; failures remain visible without
  blanking content or disabling navigation.
- Lazy widgets do not replace a page; reduced-motion placeholders do not animate.
- Dialog focus/return focus, portaled selects, inline errors and notifications work
  with keyboard input, scrolling and delayed responses in both hosts.
- English/Chinese keys remain aligned; progress/event assertions use the actual
  project and run identity.

Keep these checks about current visible behavior, not fixed test counts or old bug
reproductions. Browser checks do not establish native WebView performance, system
dialogs or packaged platform acceptance.
