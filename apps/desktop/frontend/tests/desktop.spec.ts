import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";
import type {} from "../src/runtime";

const origin = "http://127.0.0.1:19481";
const token = "temporary-fixture-token";

test("Desktop without bootstrap never becomes a browser application", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", request => {
    if (request.resourceType() === "fetch") requests.push(request.url());
  });
  await page.goto("/");
  await expect(page.locator('[data-runtime-state="pending"]')).toBeVisible();
  await expect(page.locator("#root")).toHaveText("");
  expect(requests).toEqual([]);
});

async function desktop(page: Page, pending = false) {
  await page.addInitScript(({ origin, token, pending }) => {
    localStorage.setItem("wenyi_token", "web-only-token");
    window.__WENYI_DESKTOP_PENDING__ = true;
    if (!pending) window.__WENYI_DESKTOP__ = { apiBase: origin, token };
    const get = Storage.prototype.getItem;
    Storage.prototype.getItem = function(key) {
      if (key === "wenyi_token") throw new Error("Desktop must not read the Web token");
      return get.call(this, key);
    };
  }, { origin, token, pending });
}

test("pending gate waits for ready, uses memory auth for HTTP, socket and download", async ({ page }) => {
  await desktop(page, true);
  await fakeApi(page, {}, origin);
  const requests: { url: string; auth?: string }[] = [];
  const frames: string[] = [];
  page.on("request", request => {
    if (request.resourceType() === "fetch")
      requests.push({ url: request.url(), auth: request.headers().authorization });
  });
  await page.routeWebSocket("**/ws/**", socket => {
    expect(socket.url()).toBe(`ws://127.0.0.1:19481/ws/projects/${pid}/progress`);
    socket.onMessage(data => frames.push(String(data)));
  });
  await page.goto(`/projects/${pid}`);
  await expect(page.locator('[data-runtime-state="pending"]')).toBeVisible();
  await expect(page.locator('[data-runtime-state="pending"]')).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#root")).toHaveText("");
  expect(requests).toEqual([]);
  await page.evaluate(({ origin, token }) => {
    window.__WENYI_DESKTOP__ = { apiBase: origin, token };
    window.dispatchEvent(new Event("wenyi:desktop-ready"));
  }, { origin, token });
  await expect(page.getByRole("heading", { name: "Test Book" })).toBeVisible();
  await expect.poll(() => frames.length).toBeGreaterThan(0);
  expect(JSON.parse(frames[0])).toEqual({ token });
  await page.route(`${origin}/projects/${pid}/glossary/export?format=json`, route =>
    route.fulfill({ body: "[]", headers: {
      "content-disposition": "attachment; filename=terms.json",
      "access-control-expose-headers": "Content-Disposition",
    } }));
  const download = page.waitForEvent("download");
  await page.evaluate(async pid => {
    const modulePath = "/tests/api-harness.ts";
    const { api, setAuthToken } = await import(modulePath);
    setAuthToken("must-not-persist");
    await api.downloadGlossary(pid, "json");
  }, pid);
  expect((await download).suggestedFilename()).toBe("terms.json");
  expect(requests.length).toBeGreaterThan(3);
  expect(requests.every(request => request.url.startsWith(origin + "/") &&
    !request.url.includes(token) && request.auth === `Bearer ${token}`)).toBe(true);
  expect(await page.evaluate(() => ({ ...localStorage }))).toMatchObject({ wenyi_token: "web-only-token" });
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("wenyi:desktop-error", { detail: "Worker stopped safely" })));
  await expect(page.getByRole("alert")).toContainText("Worker stopped safely");
  const count = requests.length;
  await page.waitForTimeout(3000);
  expect(requests).toHaveLength(count);
});

for (const state of ["error", "closing"] as const) {
  test(`retained ${state} before React prevents requests and never falls back to Web`, async ({ page }) => {
    await desktop(page);
    await page.addInitScript(state => {
      if (state === "error") window.__WENYI_DESKTOP_ERROR__ = "Unable to start local storage";
      else window.__WENYI_DESKTOP_CLOSING__ = true;
    }, state);
    const requests: string[] = [];
    page.on("request", request => {
      if (request.resourceType() === "fetch") requests.push(request.url());
    });
    await page.goto("/");
    if (state === "error") {
      await expect(page.getByRole("alert")).toContainText("Unable to start local storage");
      await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();
    } else {
      await expect(page.locator('[data-runtime-state="closing"]')).toBeVisible();
      await expect(page.locator("#root")).toHaveText("");
    }
    expect(requests).toEqual([]);
  });
}

test("closing stops business polling and notifications; reload retains the gate", async ({ page }) => {
  await desktop(page);
  await fakeApi(page, {}, origin);
  await page.goto(`/projects/${pid}`);
  await expect(page.getByRole("heading", { name: "Test Book" })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("wenyi:desktop-closing")));
  await expect(page.locator('[data-runtime-state="closing"]')).toBeVisible();
  await expect(page.locator("#root")).toHaveText("");
  const requests: string[] = [];
  page.on("request", request => {
    if (request.resourceType() === "fetch") requests.push(request.url());
  });
  await page.waitForTimeout(3500);
  expect(requests).toEqual([]);
  await page.addInitScript(() => { window.__WENYI_DESKTOP_CLOSING__ = true; });
  await page.reload();
  await expect(page.locator('[data-runtime-state="closing"]')).toBeVisible();
  expect(requests).toEqual([]);
});

test("reduced motion pending gate is quiet and makes no requests across reload", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await desktop(page, true);
  const requests: string[] = [];
  page.on("request", request => {
    if (request.resourceType() === "fetch") requests.push(request.url());
  });
  await page.goto("/");
  const gate = page.locator('[data-runtime-state="pending"]');
  await expect(gate).toBeVisible();
  await expect(gate).toHaveCSS("animation-name", "none");
  await expect(page.locator("#root")).toHaveText("");
  await page.reload();
  await expect(gate).toBeVisible();
  expect(requests).toEqual([]);
});

test("Desktop healthy socket reduces requests, retains reconciliation and resumes polling on disconnect", async ({ page }) => {
  await desktop(page);
  // Pause before app timers exist; a fixed future time avoids sampling a running clock.
  await page.clock.install({ time: new Date("2025-01-01T00:00:00Z") });
  await page.clock.pauseAt(new Date("2025-01-02T00:00:00Z"));
  await fakeApi(page, { [`/projects/${pid}`]: { ...project, status: "translating" } }, origin);
  let socket: WebSocketRoute;
  let accept = true;
  await page.routeWebSocket("**/ws/**", ws => {
    if (accept) socket = ws;
    else void ws.close();
  });
  const initialResponses = new Set<string>();
  const initialPaths = ["", "/chapters", "/report", "/stats", "/workflow"]
    .map(path => `/projects/${pid}${path}`);
  page.on("response", response => {
    if (response.url().startsWith(origin)) initialResponses.add(new URL(response.url()).pathname);
  });
  const counts: Record<string, number> = {};
  page.on("request", request => {
    if (request.url().startsWith(origin))
      counts[new URL(request.url()).pathname] = (counts[new URL(request.url()).pathname] || 0) + 1;
  });
  await page.goto(`/projects/${pid}`);
  // Startup queries cross process boundaries; tick their notifications until all fixtures arrive.
  await expect.poll(async () => {
    await page.clock.runFor(100);
    return initialPaths.every(path => initialResponses.has(path));
  }).toBe(true);
  await page.clock.runFor(1000);
  await expect(page.getByText("Progress connection: Live")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Test Book" })).toBeVisible();
  const total = () => Object.values(counts).reduce((a, b) => a + b, 0);
  const start = total();
  await page.clock.runFor(10_000);
  expect(total() - start).toBe(0);
  socket!.send(JSON.stringify({ kind: "stats", project_id: pid, run_id: "run-a" }));
  await page.clock.runFor(1000);
  await expect.poll(total).toBe(start + 1);
  socket!.send(JSON.stringify({ kind: "translation", project_id: pid, run_id: "run-a", label: "Saved local batch" }));
  await page.clock.runFor(1000);
  await expect(page.getByRole("status")).toContainText("Saved local batch");
  await expect.poll(total).toBe(start + 3);
  socket!.send(JSON.stringify({ kind: "translation", project_id: "other", run_id: "run-a", label: "Wrong project" }));
  socket!.send(JSON.stringify({ kind: "translation", project_id: pid, run_id: "old-run", label: "Old run" }));
  await page.clock.runFor(1000);
  await expect(page.getByRole("status")).not.toContainText("Old run");
  const beforeReconcile = total();
  await page.clock.runFor(30_000);
  expect(total()).toBeGreaterThan(beforeReconcile);
  accept = false;
  await socket!.close();
  await page.clock.runFor(100);
  await expect(page.getByText("Progress connection: Polling")).toBeVisible();
  const beforeFallback = total();
  await page.clock.runFor(10_000);
  // Drain browser-to-runner request delivery without advancing the paused clock.
  await expect.poll(() => total() - beforeFallback).toBeGreaterThanOrEqual(8);
  console.log(`Desktop fixture: quiet healthy 10s=0 requests; disconnected 10s=${total() - beforeFallback}`);
});

test("lazy route failure is visible", async ({ page }) => {
  await desktop(page);
  await fakeApi(page, {}, origin);
  await page.route("**/features/dashboard/Dashboard.tsx*", route => route.abort());
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Unable to load this page");
  await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();
});

test("lazy route pending has no visible loading copy and reload recovers a route failure", async ({ page }) => {
  await desktop(page);
  await fakeApi(page, {}, origin);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/features/dashboard/Dashboard.tsx*", async route => {
    await held;
    await route.abort();
  });
  await page.goto("/");
  await expect(page.locator("[data-route-pending]")).toHaveAttribute("aria-busy", "true");
  // The pending gate is quiet and covers only the content area; the shell stays.
  await expect(page.locator("[data-route-pending]")).toHaveText("");
  await expect(page.locator("#sidebar-navigation")).toBeVisible();
  release();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.unroute("**/features/dashboard/Dashboard.tsx*");
  await page.getByRole("button", { name: "Reload" }).click();
  await expect(page.getByRole("heading", { name: "My projects", exact: true })).toBeVisible();
});

test("an error event while the module graph loads is retained", async ({ page }) => {
  await desktop(page, true);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/src/main.tsx", async route => {
    await held;
    await route.continue();
  });
  await page.goto("/", { waitUntil: "commit" });
  await page.locator("#root").waitFor({ state: "attached" });
  await page.evaluate(() => window.dispatchEvent(
    new CustomEvent("wenyi:desktop-error", { detail: "Early startup failure" }),
  ));
  release();
  await expect(page.getByRole("alert")).toContainText("Early startup failure");
});

for (const kind of ["state", "final"]) {
  test(`Desktop ${kind} event refreshes terminal state without waiting for polling`, async ({ page }) => {
    await desktop(page);
    await fakeApi(page, { [`/projects/${pid}`]: { ...project, status: "translating" } }, origin);
    let socket!: WebSocketRoute;
    await page.routeWebSocket("**/ws/**", ws => { socket = ws; });
    await page.goto(`/projects/${pid}`);
    await expect(page.getByText("Progress connection: Live")).toBeVisible();
    await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
    await page.route(`${origin}/projects/${pid}`, route => route.fulfill({ json: project }));
    socket.send(JSON.stringify({ kind, project_id: pid, run_id: "run-a" }));
    await expect(page.getByRole("button", { name: "Start translation", exact: true })).toBeVisible();
  });
}

test("navigating between routes keeps the sidebar shell as the same node", async ({ page }) => {
  await desktop(page);
  await fakeApi(page, {}, origin);
  await page.goto("/");
  await page.locator("#sidebar-navigation").waitFor();
  await page.evaluate(() => {
    (window as unknown as { __shellNav?: Element | null }).__shellNav =
      document.querySelector("#sidebar-navigation");
  });
  await page.locator('a[href="/settings"]').click();
  await expect(page).toHaveURL("/settings");
  await expect(page.locator("#sidebar-navigation")).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __shellNav?: Element | null }).__shellNav ===
        document.querySelector("#sidebar-navigation"),
    ),
  ).toBe(true);
});

test("Desktop HTTP errors remain visible", async ({ page }) => {
  await desktop(page);
  await fakeApi(page, {}, origin);
  await page.route(`${origin}/projects`, route =>
    route.fulfill({ status: 503, json: { detail: "Local database is unavailable" } }));
  await page.goto("/");
  await expect(page.getByText("503: Local database is unavailable")).toBeVisible();
});
