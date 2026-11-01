import { expect, test, type Page, type Request } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

async function waitForProjectReconciliation(
  page: Page,
  authenticated: () => boolean,
  reconciledName: string,
) {
  // Clock advancement alone cannot drain socket events or HTTP route callbacks.
  // Observe the refreshed snapshot in the UI before measuring idle requests.
  await expect.poll(async () => {
    await page.clock.runFor(100);
    return authenticated() && await page.getByTitle(reconciledName, { exact: true }).count() === 1;
  }, { intervals: [50] }).toBe(true);
}

export function registerTests() {
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
        await waitForProjectReconciliation(page, () => authenticated, reconciledName);
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

for (const startup of [
  { name: "immediate startup", socketDelay: 0, responseDelay: 0 },
  { name: "delayed socket open", socketDelay: 3000, responseDelay: 0 },
  { name: "delayed reconciliation response", socketDelay: 0, responseDelay: 3000 },
]) {
test(`resuming an idle project restarts polling without reloading the page (${startup.name})`, async ({ page }) => {
  await page.clock.install({ time: new Date("2025-01-01T00:00:00Z") });
  if (startup.socketDelay) {
    await page.addInitScript(delay => {
      const NativeWebSocket = window.WebSocket;
      window.WebSocket = class extends NativeWebSocket {
        constructor(...args: ConstructorParameters<typeof NativeWebSocket>) {
          super(...args);
          if (!new URL(args[0], location.href).pathname.startsWith("/ws/")) return;
          // Deliver the open event after the old fixed startup allowance.
          this.addEventListener("open", event => {
            event.stopImmediatePropagation();
            setTimeout(() => this.dispatchEvent(new Event("open")), delay);
          }, { once: true });
        }
      };
    }, startup.socketDelay);
  }
  const saved = { ...project, status: "paused" };
  const reconciledName = `${project.name} (reconciled)`;
  let reads = 0;
  let authenticated = false;
  await fakeApi(page);
  await page.routeWebSocket("**/ws/**", socket => {
    socket.onMessage(() => { authenticated = true; });
  });
  await page.route(`**/api/projects/${pid}`, async route => {
    const read = ++reads;
    if (read === 2 && startup.responseDelay) {
      await page.evaluate(delay => new Promise<void>(resolve => {
        setTimeout(resolve, delay);
      }), startup.responseDelay);
    }
    return route.fulfill({
      json: { ...saved, name: read > 1 ? reconciledName : project.name },
    });
  });
  await page.route(`**/api/projects/${pid}/resume`, route => {
    saved.status = "translating";
    return route.fulfill({ json: { project_id: pid, job_id: "resumed" } });
  });
  await page.goto(`/projects/${pid}`);
  await expect(page.getByRole("button", { name: "Resume task", exact: true })).toBeVisible();
  await waitForProjectReconciliation(page, () => authenticated, reconciledName);
  const idle = reads;
  await page.clock.runFor(60_000);
  expect(reads).toBe(idle);
  await page.getByRole("button", { name: "Resume task", exact: true }).click();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  const resumed = reads;
  await page.clock.runFor(31_000);
  await expect.poll(() => reads).toBeGreaterThan(resumed);
});
}

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
}
