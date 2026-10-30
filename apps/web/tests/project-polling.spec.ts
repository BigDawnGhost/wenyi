import { expect, test, type Request } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

for (const status of ["done", "paused"]) {
  for (const route of [
    "", "/proofreading/0", "/contents", "/glossary", "/review",
    "/events", "/settings", "/style", "/subtitles",
  ]) {
    test(`${status} projects do not poll on ${route || "/overview"}`, async ({ page }) => {
      await page.clock.install();
      const saved = {
        ...project, status, fmt: route === "/subtitles" ? "srt" : "epub",
      };
      await fakeApi(page, {
        [`/projects/${pid}`]: saved,
      });
      const hasProgressSocket = !route || route === "/review";
      const reconciledName = `${project.name} (reconciled)`;
      let projectReads = 0;
      if (hasProgressSocket) {
        await page.route(`**/api/projects/${pid}`, route => route.fulfill({
          json: { ...saved, name: ++projectReads > 1 ? reconciledName : project.name },
        }));
      }
      let authenticated = false;
      await page.routeWebSocket("**/ws/**", socket => {
        socket.onMessage(() => { authenticated = true; });
      });
      const reads: string[] = [];
      const pending = new Set<Request>();
      page.on("request", request => {
        if (request.resourceType() !== "fetch" || !request.url().includes("/api/")) return;
        reads.push(request.url());
        pending.add(request);
      });
      page.on("requestfinished", request => pending.delete(request));
      page.on("requestfailed", request => pending.delete(request));
      await page.goto(`/projects/${pid}${route}`);
      await expect(page.getByRole("main").getByRole("heading").first()).toBeVisible();
      if (route === "/proofreading/0") {
        await page.getByRole("button", { name: "Paragraph actions", exact: true }).click();
        await page.getByRole("menuitem", { name: "Change history", exact: true }).click();
        await expect(page.getByRole("tab", { name: "Change history", exact: true }))
          .toHaveAttribute("aria-selected", "true");
      }
      if (hasProgressSocket) {
        await expect.poll(() => authenticated).toBe(true);
        // Authentication alone does not mean the coalesced refresh has run.
        // Wait for its saved metadata to render before measuring idle requests.
        await expect.poll(async () => {
          await page.clock.runFor(100);
          return page.getByTitle(reconciledName, { exact: true }).count();
        }).toBe(1);
      } else {
        await page.clock.runFor(1500);
      }
      await expect.poll(() => pending.size).toBe(0);
      const initial = [...reads];
      expect(initial.length).toBeGreaterThan(0);
      await page.clock.runFor(60_000);
      expect(reads).toEqual(initial);
    });
  }
}

test("a terminal project read fetches the final chapter before stopping polling", async ({ page }) => {
  await page.clock.install();
  const saved = { ...project, status: "translating" };
  let target = "Partial translation";
  let reads = 0;
  await fakeApi(page, { [`/projects/${pid}`]: saved });
  await page.route(`**/api/projects/${pid}/review/0`, route => {
    reads++;
    return route.fulfill({
      json: {
        index: 0, title: "Chapter One", review_issues: [],
        segments: [{ index: 0, source: "Source", target, kind: "text" }],
      },
    });
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect(page.getByTestId("translation-text")).toHaveText(target);
  await page.clock.runFor(4000);
  expect(reads).toBeGreaterThan(1);
  saved.status = "done";
  target = "Final saved translation";
  await page.clock.runFor(4000);
  await expect(page.getByTestId("translation-text")).toHaveText(target);
  await page.getByRole("button", { name: "Paragraph actions", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "Edit translation", exact: true })).toBeEnabled();
  await page.keyboard.press("Escape");
  await page.clock.runFor(1000);
  const finalReads = reads;
  await page.clock.runFor(60_000);
  expect(reads).toBe(finalReads);
});

test("an in-flight chapter read cannot swallow the final paused snapshot", async ({ page }) => {
  await page.clock.install();
  const saved = { ...project, status: "translating" };
  let reads = 0;
  let holding = true;
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  await fakeApi(page, { [`/projects/${pid}`]: saved });
  await page.route(`**/api/projects/${pid}/review/0`, async route => {
    reads++;
    const initial = holding;
    if (initial) await delayed;
    await route.fulfill({
      json: {
        index: 0, title: "Chapter One", review_issues: [],
        segments: [{
          index: 0, source: "Source",
          target: initial ? "Old snapshot" : "Final paused snapshot", kind: "text",
        }],
      },
    });
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  // Development StrictMode may cancel and restart the first mount's request.
  await expect.poll(() => reads).toBeGreaterThan(0);
  await expect(page.getByText("Read-only while a task runs", { exact: true })).toBeVisible();
  saved.status = "paused";
  await page.clock.runFor(4000);
  await expect(page.getByText("Read-only while a task runs", { exact: true })).toHaveCount(0);
  const beforeRelease = reads;
  holding = false;
  release();
  await expect(page.getByTestId("translation-text")).toHaveText("Final paused snapshot");
  expect(reads).toBe(beforeRelease + 1);
  await page.clock.runFor(60_000);
  expect(reads).toBe(beforeRelease + 1);
});

test("resuming an idle project restarts polling without reloading the page", async ({ page }) => {
  await page.clock.install();
  const saved = { ...project, status: "paused" };
  let reads = 0;
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}`, route => {
    reads++;
    return route.fulfill({ json: saved });
  });
  await page.route(`**/api/projects/${pid}/resume`, route => {
    saved.status = "translating";
    return route.fulfill({ json: { project_id: pid, job_id: "resumed" } });
  });
  await page.goto(`/projects/${pid}`);
  await expect(page.getByRole("button", { name: "Resume task", exact: true })).toBeVisible();
  await page.clock.runFor(1500);
  const idle = reads;
  await page.clock.runFor(60_000);
  expect(reads).toBe(idle);
  await page.getByRole("button", { name: "Resume task", exact: true }).click();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  const resumed = reads;
  await page.clock.runFor(31_000);
  await expect.poll(() => reads).toBeGreaterThan(resumed);
});

test("the dashboard does not poll when every project is complete or paused", async ({ page }) => {
  await page.clock.install();
  await fakeApi(page, {
    "/projects": [project, { ...project, id: "paused-book", status: "paused" }],
  });
  let reads = 0;
  page.on("request", request => {
    if (request.url().endsWith("/api/projects")) reads++;
  });
  await page.goto("/");
  await expect(page.getByRole("main").getByText(project.name, { exact: true })).toHaveCount(2);
  await page.clock.runFor(1500);
  const initial = reads;
  await page.clock.runFor(60_000);
  expect(reads).toBe(initial);
});

test("leaving a chapter cancels its unused in-flight content request", async ({ page }) => {
  await fakeApi(page, {
    [`/projects/${pid}/review/1`]: {
      index: 1, title: "Next chapter", review_issues: [],
      segments: [{ index: 0, source: "Next source", target: "Next translation", kind: "text" }],
    },
  });
  let aborted = 0;
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  page.on("requestfailed", request => {
    if (request.url().endsWith(`/review/0`)) aborted++;
  });
  await page.route(`**/api/projects/${pid}/review/0`, async route => {
    await delayed;
    await route.fulfill({ json: { index: 0, title: "Old chapter", segments: [], review_issues: [] } });
  });
  try {
    await page.goto(`/projects/${pid}/proofreading/0`);
    const next = page.getByRole("link", { name: "Next chapter", exact: true });
    await expect(next).toBeVisible();
    const before = aborted;
    await next.click();
    await expect(page.getByTestId("translation-text")).toHaveText("Next translation");
    await expect.poll(() => aborted).toBeGreaterThan(before);
  } finally {
    release();
  }
});
