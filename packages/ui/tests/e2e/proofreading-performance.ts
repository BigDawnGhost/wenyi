import { expect, test } from "@playwright/test";
import { chapterReaderFixture, fakeApi, mockChapter, pid, project } from "../../sandbox/fixtures";


/** Whether the Desktop host must keep running this suite as well.
 *
 * A timing benchmark. `apps/desktop/frontend/playwright.config.ts` keeps it in a separate single-worker measurement project that only runs when PLAYWRIGHT_PERF=1, so it measures the Desktop runtime's rendering budget specifically.
 */
export const desktopHostSensitive = true;

export function registerTests() {
// Synthetic, offline mixed-language text: never load a user's book.
const count = Number(process.env.PERF_PARAGRAPHS || 65000);
if (!Number.isInteger(count) || count < 1000 || count > 100000) {
  throw new Error("PERF_PARAGRAPHS must be an integer between 1000 and 100000");
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
  let windows = 0;
  page.on("request", request => {
    if (request.url().includes("/window?")) windows++;
    expect(/\/review\/\d+$/.test(new URL(request.url()).pathname)).toBe(false);
  });
  const savedProject = { ...project, status: "translating" };
  await fakeApi(page, { [`/projects/${pid}`]: savedProject });
  const rows = segments.map((s) => ({ ...s }));
  await mockChapter(page, 0, () => {
    reads++;
    return { index: 0, title: "Synthetic chapter", segments: rows, review_issues: [] };
  });
  const start = Date.now();
  await page.goto(`/projects/${pid}/proofreading/0`);
  // Measure cold startup within the existing whole-test budget, not a 5s SLA.
  // The test's 30s deadline still bounds this wait and every subsequent action.
  await expect(page.locator("#paragraph-0")).toBeVisible({ timeout: info.timeout });
  const mountedRows = await page.getByTestId("translation-text").count();
  expect(mountedRows).toBeGreaterThan(0);
  expect(mountedRows).toBeLessThan(100);
  const firstRenderMs = Date.now() - start;
  const initialRenders = await page.evaluate(() => (window as any).__perf.renders);
  const initialReads = reads;
  const initialWindows = windows;
  await expect.poll(() => reads).toBeGreaterThan(initialReads);
  // Allow the response's React commit to finish.
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const pollRenders = await page.evaluate(() => (window as any).__perf.renders) - initialRenders;
  expect(windows).toBe(initialWindows);
  await page.evaluate(() => { (window as any).__unchangedRow = document.getElementById("paragraph-1"); });
  rows[0].target = "A newly saved translation";
  await expect(page.locator("#paragraph-0").getByTestId("translation-text")).toHaveText(rows[0].target, { timeout: 8000 });
  expect(windows).toBe(initialWindows);
  expect(await page.evaluate(() => (window as any).__unchangedRow === document.getElementById("paragraph-1"))).toBe(true);
  savedProject.status = "paused";
  await expect(page.getByText("Read-only while a task runs", { exact: true })).toHaveCount(0);
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
  await expect(input).toHaveValue(rows[0].target);
  const openMs = Date.now() - openStart;
  const openRenders = await page.evaluate(() => (window as any).__perf.renders) - beforeOpen;
  const inputStart = Date.now();
  await input.fill("Unsaved mixed draft 草稿");
  await expect(input).toHaveValue("Unsaved mixed draft 草稿");
  const inputMs = Date.now() - inputStart;
  const metrics = await page.evaluate(() => (window as any).__perf);
  const result = {
    count, mountedRows, firstRenderMs, pollRenders, openRenders, openMs, inputMs,
    rafP95Ms: frames[Math.floor(frames.length * .95)], rafMaxMs: frames.at(-1),
    longTaskCount: metrics.longTasks.length,
    longTaskTotalMs: metrics.longTasks.reduce((a: number, b: number) => a + b, 0),
  };
  console.log(JSON.stringify(result));
  await info.attach("performance.json", { body: JSON.stringify(result, null, 2), contentType: "application/json" });
  expect(initialRenders).toBeGreaterThan(0);
  expect(initialRenders).toBeLessThan(100);
  expect(pollRenders).toBe(0);
  // Only the active action menu closes; unrelated paragraph rows do not render.
  expect(openRenders).toBe(1);
});

test("large chapter preserves deep links, cross-paragraph selection and drafts during updates", async ({ page }) => {
  const rows = segments.map((segment) => ({ ...segment }));
  const last = rows.length - 1;
  const linkedIndex = last - 1;
  rows[last].target = "Unique far-away translation 远处译文";
  await fakeApi(page);
  await mockChapter(page, 0, () => ({ index: 0, title: "Synthetic chapter", segments: rows, review_issues: [] }));
  let refreshStarted = false;
  let releaseOld!: () => void;
  const oldRead = new Promise<void>((resolve) => { releaseOld = resolve; });
  await page.route(`**/api/projects/${pid}/chapters/0/updates`, async (route) => {
    if (refreshStarted) return route.fallback();
    refreshStarted = true;
    const older = rows.map((row) => row.index === linkedIndex
      ? { ...row, target: "An older in-flight translation" } : row);
    const response = chapterReaderFixture(
      { index: 0, segments: older }, route.request().url(), route.request().postData()!,
    );
    await oldRead;
    await route.fulfill(response);
  });
  await page.route(`**/api/projects/${pid}/review/0/segments/${linkedIndex}`, (route) =>
    route.fulfill({ status: 409, json: { detail: "Translation changed before saving" } }),
  );
  await page.goto(`/projects/${pid}/proofreading/0?segment=${linkedIndex}`);
  const linked = page.locator(`#paragraph-${linkedIndex}`);
  await expect(linked).toBeFocused();
  await expect(linked).toBeInViewport();
  await expect.poll(() => refreshStarted).toBe(true);
  expect(await page.getByTestId("translation-text").count()).toBeLessThan(100);
  const selected = await page.evaluate(({ linkedIndex, lastIndex }) => {
    const first = document.querySelector(`#paragraph-${linkedIndex} [data-testid=translation-text]`)!;
    const last = document.querySelector(`#paragraph-${lastIndex} [data-testid=translation-text]`)!;
    const range = document.createRange();
    range.setStartBefore(first);
    range.setEndAfter(last);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    return selection.toString();
  }, { linkedIndex, lastIndex: last });
  expect(selected).toContain(rows[linkedIndex].target);
  expect(selected).toContain(rows[last].target);
  await linked.getByRole("button", { name: "Paragraph actions" }).focus();
  await page.keyboard.press("Shift+F10");
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  const input = page.getByLabel("Edit translation", { exact: true });
  await input.fill("Keep my draft 保留草稿");
  rows[linkedIndex].target = "Concurrent saved translation";
  rows[last].target = "";
  await page.getByRole("button", { name: "Save translation", exact: true }).click();
  await expect(page.locator(`#paragraph-${last}`).getByTestId("translation-text")).toHaveText("(Empty translation)");
  releaseOld();
  await expect(linked.getByTestId("translation-text")).toHaveText("Concurrent saved translation");
  await expect(input).toHaveValue("Keep my draft 保留草稿");
  await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).last().click();
  await page.locator(`#paragraph-${last}`).getByRole("button", { name: "Paragraph actions" }).click();
  await expect(page.getByRole("menuitem", { name: "Edit translation", exact: true })).toBeEnabled();
});

test("scrolling skips intervening windows and copying fills virtual selection gaps", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async (text: string) => { (window as any).__copied = text; },
    } });
  });
  await fakeApi(page);
  const offsets = new Set<number>();
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith("/window")) offsets.add(Number(url.searchParams.get("offset")));
  });
  await mockChapter(page, 0, () => ({ index: 0, title: "Synthetic chapter", segments }));
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect(page.locator("#paragraph-0")).toBeVisible();
  // Establish the first endpoint before scrolling. It must remain mounted, not
  // cause every paragraph between it and the viewport to be fetched or mounted.
  await page.locator("#paragraph-0 [data-reader-text=target]").evaluate((el) => {
    const range = document.createRange();
    range.setStart(el.firstChild!, 0);
    range.setEnd(el.firstChild!, 5);
    const selection = getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
  });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  await page.locator('[data-slot="content"]').evaluate((el) => { el.scrollTop = 40000; });
  await expect.poll(() => page.locator('[data-paragraph-position]').evaluateAll((rows) =>
    rows.some((el) => Number((el as HTMLElement).dataset.paragraphPosition) > 150 &&
      !!el.querySelector("[data-reader-text]")),
  )).toBe(true);
  await expect(page.locator("#paragraph-0")).toHaveCount(1);
  expect(await page.locator("#paragraph-100").count()).toBe(0);
  expect(offsets.has(96)).toBe(false);
  expect(await page.getByTestId("translation-text").count()).toBeLessThan(100);
  await page.evaluate(() => {
    const first = document.querySelector("#paragraph-0 [data-reader-text=target]")!;
    const last = [...document.querySelectorAll<HTMLElement>("[data-paragraph-position]")]
      .find(el => Number(el.dataset.paragraphPosition) > 150 && el.querySelector("[data-reader-text=source]"))!
      .querySelector("[data-reader-text=source]")!;
    const range = document.createRange();
    range.setStart(first.firstChild!, 0);
    range.setEnd(last.firstChild!, 10);
    const selection = getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
  });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  await page.evaluate(() => document.dispatchEvent(new ClipboardEvent("copy", { cancelable: true })));
  await expect.poll(() => page.evaluate(() => (window as any).__copied || "")).toContain(segments[100].target);
  expect(await page.locator("#paragraph-100").count()).toBe(0);
});
}
