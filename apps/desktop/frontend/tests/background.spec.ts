import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { fakeApi, pid, project, workflow } from "./fixtures";
import type {} from "../src/runtime";

const origin = "http://127.0.0.1:19481";

async function bootstrap(page: Page, background = false) {
  await page.clock.install({ time: new Date("2025-01-01T00:00:00Z") });
  await page.clock.pauseAt(new Date("2025-01-02T00:00:00Z"));
  await page.addInitScript(({ origin, background }) => {
    window.__WENYI_DESKTOP__ = { apiBase: origin, token: "background-fixture-token" };
    window.__WENYI_DESKTOP_BACKGROUND__ = background;
  }, { origin, background });
}

async function setBackground(page: Page, background: boolean) {
  await page.evaluate(background => {
    window.__WENYI_DESKTOP_BACKGROUND__ = background;
    window.dispatchEvent(new Event("wenyi:desktop-visibility"));
  }, background);
  await page.clock.runFor(100);
}

test("native background suspends polling and socket refetch, then reconciles saved completion", async ({ page }) => {
  await bootstrap(page);
  const savedProject = { ...project, status: "translating" };
  await fakeApi(page, { [`/projects/${pid}`]: savedProject }, origin);
  let socket: WebSocketRoute;
  let connections = 0;
  await page.routeWebSocket("**/ws/**", ws => {
    connections++;
    socket = ws;
  });
  const requests: string[] = [];
  const responses = new Set<string>();
  let statsResponses = 0;
  page.on("request", request => {
    if (request.url().startsWith(origin)) requests.push(new URL(request.url()).pathname);
  });
  page.on("response", response => {
    if (response.url().startsWith(origin)) {
      responses.add(new URL(response.url()).pathname);
      if (new URL(response.url()).pathname.endsWith("/stats")) statsResponses++;
    }
  });
  await page.goto(`/projects/${pid}`);
  await expect.poll(async () => {
    await page.clock.runFor(100);
    return ["", "/chapters", "/report", "/stats", "/workflow"]
      .every(path => responses.has(`/projects/${pid}${path}`));
  }).toBe(true);
  await page.clock.runFor(1000);
  await expect(page.getByText("Progress connection: Live")).toBeVisible();
  expect(await page.evaluate(() => document.visibilityState)).toBe("visible");

  const statsReads = () => requests.filter(path => path.endsWith("/stats")).length;
  const beforeBriefHide = statsReads();
  const responsesBeforeBriefHide = statsResponses;
  socket!.send(JSON.stringify({
    kind: "chapter", project_id: pid, run_id: "run-a", label: "Queued foreground update",
  }));
  await expect.poll(async () => {
    await page.clock.runFor(10);
    return page.getByRole("status").textContent();
  }).toContain("Queued foreground update");
  await setBackground(page, true);
  await setBackground(page, false);
  await expect.poll(() => statsResponses).toBe(responsesBeforeBriefHide + 1);
  await page.clock.runFor(1000);
  await expect.poll(statsReads).toBe(beforeBriefHide + 1);

  // A coalesced refresh scheduled before hiding must not escape into the background.
  socket!.send(JSON.stringify({ kind: "stats", project_id: pid, run_id: "run-a" }));
  await page.clock.runFor(100);
  await setBackground(page, true);
  const before = requests.length;
  const connectedBefore = connections;
  for (const kind of ["stats", "translation", "chapter", "state", "final"]) {
    socket!.send(JSON.stringify({
      kind, project_id: pid, run_id: "run-a", label: "Background batch",
    }));
  }
  await page.clock.runFor(120_000);
  expect(requests).toHaveLength(before);
  expect(connections).toBe(connectedBefore);
  await expect(page.getByRole("status")).not.toContainText("Background batch");
  await expect(page.locator('[data-runtime-state="closing"]')).toHaveCount(0);

  savedProject.name = "Finished in background";
  savedProject.status = "done";
  await setBackground(page, false);
  await expect.poll(async () => {
    await page.clock.runFor(100);
    return page.getByRole("heading", { name: "Finished in background" }).count();
  }).toBe(1);
  expect(requests.length).toBeGreaterThan(before);
  expect(connections).toBe(connectedBefore);
});

test("restoring follows an in-flight pre-hide snapshot with a fresh request", async ({ page }) => {
  await bootstrap(page);
  await fakeApi(page, {}, origin);
  const savedProject = { ...project, status: "translating" };
  let holdNext = false;
  let held = false;
  let reads = 0;
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  await page.route(`${origin}/projects/${pid}`, async route => {
    reads++;
    const snapshot = { ...savedProject };
    if (holdNext) {
      holdNext = false;
      held = true;
      await delayed;
    }
    await route.fulfill({ json: snapshot });
  });
  let socket: WebSocketRoute;
  await page.routeWebSocket("**/ws/**", ws => { socket = ws; });
  await page.goto(`/projects/${pid}`);
  await expect.poll(async () => {
    await page.clock.runFor(100);
    return page.getByRole("heading", { name: "Test Book" }).count();
  }).toBe(1);
  await page.clock.runFor(1000);
  await expect(page.getByText("Progress connection: Live")).toBeVisible();
  holdNext = true;
  socket!.send(JSON.stringify({ kind: "state", project_id: pid, run_id: "run-a" }));
  await page.clock.runFor(1000);
  try {
    await expect.poll(() => held).toBe(true);
    await setBackground(page, true);
    savedProject.name = "Fresh completed snapshot";
    savedProject.status = "done";
    await setBackground(page, false);
    const beforeRelease = reads;
    release();
    // No 30-second polling tick: the old response must trigger a trailing read.
    await expect.poll(async () => {
      await page.clock.runFor(100);
      return page.getByRole("heading", { name: "Fresh completed snapshot" }).count();
    }).toBe(1);
    expect(reads).toBeGreaterThan(beforeRelease);
  } finally {
    release();
  }
});

for (const initiallyBackground of [false, true]) {
  test(`all-page polling respects native state, including retained background=${initiallyBackground}`, async ({ page }) => {
    await bootstrap(page, initiallyBackground);
    await fakeApi(page, {}, origin);
    let reads = 0;
    await page.route(`${origin}/projects`, route => {
      reads++;
      return route.fulfill({ json: [project] });
    });
    await page.goto("/");
    await expect.poll(async () => {
      await page.clock.runFor(100);
      // Scope to main: the sidebar also recalls the first project by name.
      return page.getByRole("main").getByText("Test Book", { exact: true }).count();
    }).toBe(1);
    await page.clock.runFor(1000);
    if (!initiallyBackground) {
      // Ordinary loss of focus is not native minimization or hiding.
      await page.evaluate(() => window.dispatchEvent(new Event("blur")));
      const beforeBlur = reads;
      await page.clock.runFor(6000);
      await expect.poll(() => reads).toBeGreaterThan(beforeBlur);
      await setBackground(page, true);
    }
    const before = reads;
    await page.clock.runFor(60_000);
    expect(reads).toBe(before);
    await setBackground(page, false);
    await expect.poll(() => reads).toBe(before + 1);
    // Repeated native foreground events must not create extra reconciliation requests.
    await setBackground(page, false);
    expect(reads).toBe(before + 1);
  });
}

test("hiding and restoring preserves an unsaved paragraph draft", async ({ page }) => {
  await bootstrap(page);
  await fakeApi(page, {}, origin);
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect.poll(async () => {
    await page.clock.runFor(100);
    return page.getByTestId("translation-text").count();
  }).toBe(1);
  await page.getByRole("button", { name: "Paragraph actions", exact: true }).click();
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  const draft = page.getByRole("dialog").getByRole("textbox");
  await draft.fill("Unsaved local draft");
  await setBackground(page, true);
  await page.clock.runFor(60_000);
  await expect(draft).toHaveValue("Unsaved local draft");
  await setBackground(page, false);
  await page.clock.runFor(1000);
  await expect(draft).toHaveValue("Unsaved local draft");
});

for (const kind of ["translation", "review"] as const) {
  test(`${kind} elapsed-time rendering pauses in the background and resumes`, async ({ page }) => {
    await bootstrap(page);
    const updated = "2025-01-02T00:00:00Z";
    const run = {
      id: "review-current", review_id: "review-current", status: "running",
      created_at: updated, summary: {}, issues: [], changes: [], autofix: {},
      result: { started_at: updated }, items: [],
    };
    await fakeApi(page, {
      [`/projects/${pid}`]: {
        ...project, status: kind === "review" ? "reviewing" : "translating",
      },
      [`/projects/${pid}/stats`]: {
        usage: { totals: { total_tokens: 100, calls: 1 } },
        timing: {
          total_seconds: 40,
          runs: [{
            id: "run-a", operation: "workflow", status: "running",
            started_at: updated, elapsed_seconds: 40,
          }],
        },
        live: { run_id: "run-a", updated_at: updated, valid_for_seconds: 120 },
      },
      [`/projects/${pid}/review/runs`]: [run],
      [`/projects/${pid}/review/runs/${run.id}`]: run,
      [`/projects/${pid}/workflow`]: {
        ...workflow, kind, status: "running", review_id: run.id,
        progress: {
          kind, project_id: pid, run_id: "run-a", label: "Whole-book review R1",
          done: 37, total: 100, elapsed_seconds: 40, updated_at: updated,
        },
      },
    }, origin);
    await page.goto(`/projects/${pid}${kind === "review" ? "/review" : ""}`);
    const elapsed = kind === "review"
      ? page.getByTestId("review-elapsed")
      : page.getByRole("region", { name: "Total usage & run time" }).locator("dl");
    await expect.poll(async () => {
      await page.clock.runFor(100);
      return await elapsed.count() ? elapsed.textContent() : "";
    }).toMatch(/\d+\s?s/);
    const initial = await elapsed.textContent();
    await page.clock.runFor(2000);
    await expect(elapsed).not.toHaveText(initial!);
    await setBackground(page, true);
    const paused = await elapsed.textContent();
    await page.clock.runFor(60_000);
    await expect(elapsed).toHaveText(paused!);
    await setBackground(page, false);
    await expect(elapsed).not.toHaveText(paused!);
  });
}
