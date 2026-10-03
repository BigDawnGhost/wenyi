import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

// Synthetic, offline mixed-language text: never load a user's book.
const count = Number(process.env.PERF_PARAGRAPHS || 2000);
if (!Number.isInteger(count) || count < 1000 || count > 5000) {
  throw new Error("PERF_PARAGRAPHS must be an integer between 1000 and 5000");
}
const segments = Array.from({ length: count }, (_, index) => ({
  index,
  kind: "text",
  source: `${index}: ${"The river crosses the quiet forest. 河流穿过宁静的树林。 ".repeat(12)}`,
  target: `${index}: ${"清晨的阳光照亮河岸。 Morning light reaches the riverbank. ".repeat(12)}`,
}));

test("large chapter reading, polling and editing measurements", async ({ page }, info) => {
  page.on("pageerror", (error) => console.error(error));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.addInitScript(() => {
    const metrics = { longTasks: [] as number[], renders: 0 };
    (window as any).__perf = metrics;
    new PerformanceObserver((list) => {
      metrics.longTasks.push(...list.getEntries().map((entry) => entry.duration));
    }).observe({ type: "longtask", buffered: true });
    // React's development hook observes actual committed ParagraphActions work,
    // without production instrumentation or timing every row during rendering.
    const seen = new WeakMap<object, number>();
    (window as any).__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
      supportsFiber: true,
      renderers: new Map(),
      inject: () => 1,
      onCommitFiberRoot: (_id: number, root: any) => {
        const visit = (fiber: any) => {
          if (!fiber) return;
          if (fiber.type?.name === "ParagraphActions" && (fiber.flags & 1) &&
              seen.get(fiber) !== fiber.actualStartTime) {
            metrics.renders++;
            seen.set(fiber, fiber.actualStartTime);
          }
          visit(fiber.child);
          visit(fiber.sibling);
        };
        visit(root.current);
      },
      onCommitFiberUnmount: () => {},
    };
  });
  let reads = 0;
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}/review/0`, (route) => {
    reads++;
    return route.fulfill({ json: { index: 0, title: "Synthetic chapter", segments, review_issues: [] } });
  });
  const start = Date.now();
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect(page.locator("#paragraph-0")).toBeVisible();
  await expect(page.getByTestId("translation-text")).toHaveCount(count);
  const firstRenderMs = Date.now() - start;
  const initialRenders = await page.evaluate(() => (window as any).__perf.renders);
  const initialReads = reads;
  await expect.poll(() => reads).toBeGreaterThan(initialReads);
  // Allow the response's React commit to finish.
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const pollRenders = await page.evaluate(() => (window as any).__perf.renders) - initialRenders;
  const frames = await page.locator("#paragraph-0").evaluate(async (row) => {
    let scroller = row.parentElement!;
    while (scroller.parentElement && scroller.scrollHeight <= scroller.clientHeight) scroller = scroller.parentElement;
    const intervals: number[] = [];
    let previous = performance.now();
    for (let i = 0; i < 120; i++) {
      await new Promise<void>((resolve) => requestAnimationFrame((now) => {
        intervals.push(now - previous);
        previous = now;
        scroller.scrollTop += 60;
        resolve();
      }));
    }
    scroller.scrollTop = 0;
    return intervals.slice(1).sort((a, b) => a - b);
  });
  await page.locator("#paragraph-0").scrollIntoViewIfNeeded();
  await page.locator("#paragraph-0").getByRole("button", { name: "Paragraph actions" }).click();
  const beforeOpen = await page.evaluate(() => (window as any).__perf.renders);
  const openStart = Date.now();
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  const input = page.getByLabel("Edit translation", { exact: true });
  await expect(input).toHaveValue(segments[0].target);
  const openMs = Date.now() - openStart;
  const openRenders = await page.evaluate(() => (window as any).__perf.renders) - beforeOpen;
  const inputStart = Date.now();
  await input.fill("Unsaved mixed draft 草稿");
  await expect(input).toHaveValue("Unsaved mixed draft 草稿");
  const inputMs = Date.now() - inputStart;
  const metrics = await page.evaluate(() => (window as any).__perf);
  const result = {
    count, firstRenderMs, pollRenders, openRenders, openMs, inputMs,
    rafP95Ms: frames[Math.floor(frames.length * .95)], rafMaxMs: frames.at(-1),
    longTaskCount: metrics.longTasks.length,
    longTaskTotalMs: metrics.longTasks.reduce((a: number, b: number) => a + b, 0),
  };
  console.log(JSON.stringify(result));
  await info.attach("performance.json", { body: JSON.stringify(result, null, 2), contentType: "application/json" });
  expect(initialRenders).toBeGreaterThanOrEqual(count);
  expect(pollRenders).toBe(0);
  // Only the active action menu closes; unrelated paragraph rows do not render.
  expect(openRenders).toBe(1);
});

test("large chapter preserves deep links, find, cross-paragraph selection and drafts during updates", async ({ page }) => {
  const rows = segments.slice(0, 1000).map((segment) => ({ ...segment }));
  rows[999].target = "Unique far-away translation 远处译文";
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}/review/0`, (route) =>
    route.fulfill({ json: { index: 0, title: "Synthetic chapter", segments: rows, review_issues: [] } }),
  );
  await page.goto(`/projects/${pid}/proofreading/0?segment=998`);
  const linked = page.locator("#paragraph-998");
  await expect(linked).toBeFocused();
  await expect(linked).toBeInViewport();
  // Chromium's native find implementation must discover text in skipped content.
  expect(await page.evaluate(() => (window as any).find("Unique far-away translation"))).toBe(true);
  await expect(page.locator("#paragraph-999")).toBeInViewport();
  const selected = await page.evaluate(() => {
    const first = document.querySelector("#paragraph-998 [data-testid=translation-text]")!;
    const last = document.querySelector("#paragraph-999 [data-testid=translation-text]")!;
    const range = document.createRange();
    range.setStartBefore(first);
    range.setEndAfter(last);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    return selection.toString();
  });
  expect(selected).toContain(rows[998].target);
  expect(selected).toContain(rows[999].target);
  await linked.getByRole("button", { name: "Paragraph actions" }).focus();
  await page.keyboard.press("Shift+F10");
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  const input = page.getByLabel("Edit translation", { exact: true });
  await input.fill("Keep my draft 保留草稿");
  rows[999].target = "";
  await expect(page.locator("#paragraph-999").getByTestId("translation-text")).toHaveText("(Empty translation)");
  await expect(input).toHaveValue("Keep my draft 保留草稿");
  await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).last().click();
  await page.locator("#paragraph-999").getByRole("button", { name: "Paragraph actions" }).click();
  await expect(page.getByRole("menuitem", { name: "Edit translation", exact: true })).toBeEnabled();
});
