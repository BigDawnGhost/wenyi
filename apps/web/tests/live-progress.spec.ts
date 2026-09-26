import { expect, test, type WebSocketRoute } from "@playwright/test";
import { fakeApi, pid, project, chapter, workflow } from "./fixtures";

test("run time ticks during a long batch and stops on pause without reloading", async ({
  page,
}) => {
  await page.clock.install();
  let paused = false;
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({
      json: {
        ...project,
        status: paused ? "paused" : "translating",
      },
    }),
  );
  await page.route(`**/api/projects/${pid}/stats`, (route) =>
    route.fulfill({
      json: {
        usage: { totals: { total_tokens: 15, calls: 1 } },
        timing: {
          total_seconds: paused ? 27 : 22,
          runs: [
            {
              id: "old",
              elapsed_seconds: 20,
              status: "completed",
              operation: "prepare",
            },
            {
              id: "active",
              elapsed_seconds: paused ? 7 : 2,
              status: paused ? "interrupted" : "running",
              operation: "workflow",
            },
          ],
        },
        live: paused
          ? null
          : {
              run_id: "run-a",
              updated_at: "2026-09-23T00:00:00Z",
              valid_for_seconds: 10,
            },
      },
    }),
  );
  await page.goto(`/projects/${pid}`);
  const metrics = page
    .getByRole("region", { name: "Total usage & run time" })
    .locator("dl");
  await expect(metrics).toContainText("22 s");
  await page.clock.runFor(3000);
  await expect(metrics).toContainText("25 s");
  paused = true;
  await page.clock.runFor(3000);
  await expect(metrics).toContainText("27 s");
  await page.clock.runFor(20000);
  await expect(metrics).toContainText("27 s");
});

test("socket events refresh saved batch counts, usage, and final state", async ({
  page,
}) => {
  await page.clock.install();
  await fakeApi(page);
  let finished = false;
  let saved = 0;
  let tokens = 10;
  let socket: WebSocketRoute | undefined;
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({
      json: {
        ...project,
        status: finished ? "done" : "translating",
      },
    }),
  );
  await page.route(`**/api/projects/${pid}/chapters`, (route) =>
    route.fulfill({
      json: [
        {
          ...chapter,
          target_word_count: saved,
          status: finished ? "done" : "translating",
        },
      ],
    }),
  );
  await page.route(`**/api/projects/${pid}/workflow`, (route) =>
    route.fulfill({
      json: {
        ...workflow,
        status: finished ? "done" : "running",
      },
    }),
  );
  await page.route(`**/api/projects/${pid}/stats`, (route) =>
    route.fulfill({
      json: {
        usage: { totals: { total_tokens: tokens, calls: 1 } },
        timing: { total_seconds: 5, runs: [] },
      },
    }),
  );
  await page.routeWebSocket("**/ws/projects/*/progress", (ws) => {
    socket = ws;
  });
  await page.goto(`/projects/${pid}`);
  await expect(page.getByText("0 / 12", { exact: true })).toBeVisible();
  const metrics = page
    .getByRole("region", { name: "Total usage & run time" })
    .locator("dl");
  await expect(metrics).toContainText("10");
  await expect.poll(() => !!socket).toBe(true);
  saved = 4;
  tokens = 1234;
  socket!.send(
    JSON.stringify({
      kind: "translation",
      project_id: pid,
      run_id: "run-a",
      label: "Batch saved",
      done: 4,
      total: 12,
    }),
  );
  await page.clock.runFor(1000);
  await expect(page.getByText("4 / 12", { exact: true })).toBeVisible();
  await expect(metrics).toContainText("1,234");
  await expect(page.getByRole("status")).toContainText("Batch saved");
  // Background statistics updates must not replace the latest stage label.
  tokens = 2345;
  socket!.send(
    JSON.stringify({ kind: "stats", project_id: pid, run_id: "run-a" }),
  );
  await page.clock.runFor(1000);
  await expect(metrics).toContainText("2,345");
  await expect(page.getByRole("status")).toContainText("Batch saved");
  finished = true;
  saved = 12;
  tokens = 3456;
  socket!.send(
    JSON.stringify({ kind: "state", project_id: pid, run_id: "run-a" }),
  );
  await page.clock.runFor(1000);
  await expect(page.getByText("12 / 12", { exact: true })).toBeVisible();
  await expect(metrics).toContainText("3,456");
  await expect(
    page.getByRole("button", { name: "Start translation", exact: true }),
  ).toBeVisible();
  socket!.send(
    JSON.stringify({
      kind: "translation",
      project_id: "other",
      run_id: "run-a",
      label: "Wrong book",
    }),
  );
  socket!.send(
    JSON.stringify({
      kind: "translation",
      project_id: pid,
      run_id: "old-run",
      label: "Old run",
    }),
  );
  await page.clock.runFor(1000);
  await expect(page.getByRole("status")).not.toContainText("Wrong book");
  await expect(page.getByRole("status")).not.toContainText("Old run");
});

test("expired telemetry freezes the clock and a resumed run keeps cumulative time", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-09-23T00:00:00Z") });
  await fakeApi(page);
  let resumed = false;
  let resumeRequested = false;
  let releaseResume!: () => void;
  const resumeResponse = new Promise<void>((resolve) => {
    releaseResume = resolve;
  });
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ json: { ...project, status: "translating" } }),
  );
  await page.route(`**/api/projects/${pid}/stats`, async (route) => {
    if (resumed) {
      resumeRequested = true;
      await resumeResponse;
    }
    await route.fulfill({
      json: {
        timing: {
          total_seconds: resumed ? 41 : 22,
          runs: [
            {
              id: "previous",
              elapsed_seconds: resumed ? 40 : 20,
              status: "completed",
            },
            {
              id: resumed ? "new" : "active",
              elapsed_seconds: resumed ? 1 : 2,
              status: "running",
            },
          ],
        },
        live: {
          run_id: resumed ? "new-run" : "run-a",
          updated_at: resumed ? "2026-09-23T00:01:00Z" : "2026-09-23T00:00:00Z",
          valid_for_seconds: 10,
        },
      },
    });
  });
  await page.goto(`/projects/${pid}`);
  const totals = page
    .getByRole("region", { name: "Total usage & run time" })
    .locator("dl");
  await expect(totals).toContainText("22 s");
  // Let the page load normally, then expire the heartbeat and freeze real-time ticking.
  await page.clock.pauseAt(new Date("2026-09-23T00:01:00Z"));
  await expect(totals).toContainText("32 s");
  await page.clock.runFor(12000);
  await expect(totals).toContainText("32 s");
  resumed = true;
  // Hold the new snapshot until polling has run and the clock is stationary again.
  await expect
    .poll(async () => {
      await page.clock.runFor(5000);
      return resumeRequested;
    })
    .toBe(true);
  releaseResume();
  await expect
    .poll(async () => {
      // Deliver query notifications without advancing the resumed run's clock.
      await page.clock.runFor(0);
      return totals.textContent();
    })
    .toContain("41 s");
  await page.clock.runFor(3000);
  await expect(totals).toContainText("44 s");
});

test("newer cached progress wins and reconnecting refreshes without a page reload", async ({
  page,
}) => {
  await page.clock.install();
  await fakeApi(page);
  let latest = "Waiting";
  const sockets: WebSocketRoute[] = [];
  const auth: unknown[] = [];
  await page.route(`**/api/projects/${pid}/workflow`, (route) =>
    route.fulfill({
      json: {
        ...workflow,
        status: "running",
        progress: {
          kind: "translation",
          project_id: pid,
          run_id: "run-a",
          label: latest,
          updated_at:
            latest === "Waiting"
              ? "2026-09-23T00:00:00Z"
              : "2026-09-23T00:00:02Z",
        },
      },
    }),
  );
  await page.routeWebSocket("**/ws/projects/*/progress", (ws) => {
    sockets.push(ws);
    ws.onMessage((data) => auth.push(JSON.parse(String(data))));
  });
  await page.goto(`/projects/${pid}`);
  await expect(page.getByRole("status")).toContainText("Waiting");
  await expect.poll(() => auth.length).toBe(1);
  const current = sockets[sockets.length - 1];
  current.send(
    JSON.stringify({
      kind: "translation",
      project_id: pid,
      run_id: "run-a",
      label: "Older socket stage",
      updated_at: "2026-09-23T00:00:01Z",
    }),
  );
  await page.clock.runFor(100);
  await expect(page.getByRole("status")).toContainText("Older socket stage");
  latest = "Newer persisted stage";
  await page.clock.runFor(3000);
  await expect(page.getByRole("status")).toContainText(latest);
  await current.close();
  await page.clock.runFor(100);
  await expect(page.getByText("Progress connection: Polling")).toBeVisible();
  latest = "Recovered stage";
  await page.clock.runFor(1500);
  await expect.poll(() => auth.length).toBe(2);
  await expect(page.getByRole("status")).toContainText(latest);
});
