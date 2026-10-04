# Frontend platform split verification

The Web and Desktop production entries now configure the same `PlatformServices`
contract before mounting the shared React application. Native IPC, lifecycle
bootstrap, credential APIs and credential copy are Desktop-only modules. The Web
build rejects native modules and native credential text in its actual output.

## Commands and results

Run from the repository root:

```sh
pnpm install --frozen-lockfile
pnpm -C apps/web typecheck
pnpm -C apps/desktop/frontend typecheck
pnpm -C apps/web exec tsc -p tests/tsconfig.json
pnpm -C apps/desktop/frontend exec tsc -p tests/tsconfig.json
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium pnpm -C apps/web test:e2e --workers=4
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium pnpm -C apps/desktop/frontend test:e2e --workers=4
PERF_PARAGRAPHS=5000 PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium pnpm -C apps/web test:e2e tests/proofreading-performance.spec.ts --workers=1
pnpm -C apps/web build
pnpm -C apps/desktop/frontend build
pnpm test:frontend-boundaries
```

- Frozen workspace installation and both source/test typechecks passed.
- Web E2E: **101 passed**.
- Desktop E2E against its own Vite server: **29 passed**.
- The 5,000-paragraph proofreading run: **2 passed**. Both the 2,000- and
  5,000-paragraph checks retained `pollRenders=0` and `openRenders=1`.
- The two initially failing sidebar checks measured the full-width lazy-route
  placeholder. They now wait for the actual expanded sidebar before measuring;
  no sidebar layout behavior was changed.
- The Web Docker COPY inputs were reproduced in an isolated scratch workspace
  without a Desktop package. Offline frozen filtered installation and Web build
  succeeded. No Docker image or container runtime was exercised.

## Chunk accounting

Before migration, `pnpm -C apps/web build` built one mixed-platform entry.
The table records Vite's decimal kB, uncompressed JavaScript / gzip JavaScript.
These are emitted chunk sizes, not measured startup time or total network bytes.

| Chunk | Mixed-platform baseline | Split Web | Split Desktop |
| --- | ---: | ---: | ---: |
| Entry | 423.59 / 133.86 | 419.37 / 131.47 | 425.61 / 133.82 |
| CreateProject | 10.39 / 3.86 | 9.58 / 3.49 | 9.58 / 3.50 |
| ExportPage | 7.29 / 2.67 | 7.02 / 2.52 | 7.02 / 2.52 |
| InterfaceSettingsPage | 15.52 / 4.85 | 12.81 / 4.16 | 12.81 / 4.16 |
| ProofreadingPage | 19.97 / 6.45 | 19.97 / 6.45 | 19.97 / 6.45 |
| DesktopCredential | included in settings | absent | 2.78 / 1.11 |

The native credential field remains lazy. Desktop has an intentionally larger
entry than Web because it owns the native capabilities and startup gate.
This split is an isolation change, not a claim of a new performance improvement.

**Final asset verification passed:** the original brand PNG was moved unchanged
to `packages/ui/src/assets/wenyi-emblem.png`. Both production builds completed
without missing-asset warnings and emitted the same 623.72 kB image.
`pnpm test:frontend-boundaries` passed all three checks, including the assertions
that both builds contain the shared PNG. The table above records these complete
builds; the image is not omitted to reduce the reported output size.

The optional production comparison script is now
`apps/desktop/frontend/tests/measure-desktop.mjs`; it accepts separate Web and
Desktop output directories. It reports transferred JavaScript without asserting
that Desktop must be smaller than Web.
