import { expect, test, type WebSocketRoute } from "@playwright/test";
import { createTranslator } from "../../src/i18n/catalog";
import { fakeApi, pid, project, workflow } from "../../sandbox/fixtures";
import { expectTokenColor } from "./color-helper";


/** Whether the Desktop host must keep running this suite as well.
 *
 * It drives `progressInterval`, `connectProgress` and `activity`, so the Desktop-only 5s connected interval and the foreground gate are both under it. The layout assertions are shared but the data cadence beneath them is not.
 */
export const desktopHostSensitive = true;

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
for (const locale of ["en", "zh-CN"] as const) {
  const t = createTranslator(locale);
  const stage = locale === "en" ? "Translating chapter 3" : "正在翻译第 3 章";

  test.describe(`flat translation progress in ${locale}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript((locale) => {
        localStorage.setItem("wenyi.locale", locale);
      }, locale);
      await fakeApi(page, {}, apiOrigin);
    });

    for (const theme of ["light", "dark"] as const) {
      test(`task controls stay quiet and keyboard-operable in ${theme}`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.addInitScript(theme => localStorage.setItem("wenyi.theme", theme), theme);
        const apiBase = apiOrigin ?? "**/api";
        let status = "ready";
        await page.route(`${apiBase}/projects/${pid}`, route =>
          route.fulfill({
            json: {
              ...project,
              name: "尤利西斯",
              title: "Ulysses",
              source_lang: "en",
              target_lang: "zh",
              status,
            },
          }),
        );
        const requests: string[] = [];
        let pendingRequest = Promise.resolve();
        await page.route(new RegExp(`/projects/${pid}/(translate|pause|resume)$`), async route => {
          // Web prefixes requests with /api; Desktop addresses its local API directly.
          requests.push(new URL(route.request().url()).pathname.replace(/^\/api(?=\/)/, ""));
          await pendingRequest;
          await route.fulfill({ json: { project_id: pid, task_id: "task-control", message: "Accepted" } });
        });
        const header = page.locator('[data-slot="page.header"]');
        for (const [state, command, label] of [
          ["ready", "translate", "common.startTranslation"],
          ["translating", "pause", "progress.pause"],
          ["paused", "resume", "progress.resumeTask"],
          ["error", "resume", "progress.resumeTask"],
        ] as const) {
          status = state;
          await page.goto(`/projects/${pid}`);
          const button = header.getByRole("button", { name: t(label), exact: true });
          await expect(button).toBeEnabled();
          await expect(button).toHaveText("");
          await expect(button).toHaveAttribute("title", t(label));
          await expect(button).toHaveCSS("width", "32px");
          await expect(button).toHaveCSS("height", "32px");
          await expect(button).toHaveCSS("border-top-width", "1px");
          await expectTokenColor(button, "border-top-color", "border");
          await expect(button).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
          await expect(button).toHaveCSS("background-image", "none");
          await expect(button).toHaveCSS("--tw-shadow", "0 0 #0000");
          await expectTokenColor(button, "color", "muted-foreground");
          await expect(button.locator("svg")).toHaveAttribute("aria-hidden", "true");
          if (locale === "zh-CN" && state !== "error") {
            await header.screenshot({ path: testInfo.outputPath(`task-control-${state}-${theme}.png`) });
          }
          await button.hover();
          await expectTokenColor(button, "background-color", "hover");
          await page.mouse.move(0, 0);
          requests.length = 0;
          let release!: () => void;
          pendingRequest = new Promise(resolve => { release = resolve; });
          try {
            await button.press("Enter");
            await expect.poll(() => requests).toEqual([`/projects/${pid}/${command}`]);
            await expect(button).toBeEnabled();
            await expect(button).toHaveAttribute("aria-busy", "true");
            await expect(button.locator("svg")).not.toHaveClass(/animate-spin/);
            await page.keyboard.press("Enter");
            expect(requests).toHaveLength(1);
          } finally {
            release();
          }
          await expect(button).toBeEnabled();
          await expect(button).not.toHaveAttribute("aria-busy", "true");
        }
        status = "pausing";
        await page.goto(`/projects/${pid}`);
        const pausing = header.getByRole("button", { name: t("progress.pause"), exact: true });
        await expect(pausing).toHaveAttribute("aria-busy", "true");
        requests.length = 0;
        await pausing.press("Enter");
        expect(requests).toEqual([]);
        status = "parsing";
        await page.goto(`/projects/${pid}`);
        await expect(page.getByRole("heading", { name: "尤利西斯", exact: true })).toBeVisible();
        await expect(header.getByRole("button")).toHaveCount(0);
      });
    }

    for (const status of ["parsing", "uploaded", "preparing", "error"]) {
      test(`${status} projects do not offer source upload before AI preparation`, async ({ page }) => {
        const apiBase = apiOrigin ?? "**/api";
        await page.route(`${apiBase}/projects/${pid}`, (route) =>
          route.fulfill({ json: { ...project, status, initialized: false } }),
        );
        if (status === "parsing") {
          await page.route(`${apiBase}/projects/${pid}/chapters`, (route) =>
            route.fulfill({ json: [] }),
          );
        }
        await page.goto(`/projects/${pid}`);
        await expect(page.getByRole("heading", { name: project.name, exact: true }))
          .toBeVisible();
        await expect(page.getByRole("link", { name: /Upload & preview source|上传与预览原文/ }))
          .toHaveCount(0);
        if (status !== "parsing") {
          await expect(page.getByRole("link", { name: t("contents.readContentsSource") }))
            .toBeVisible();
        }
      });
    }

    test("renders cached and live pipeline stage keys without losing the workflow", async ({ page }) => {
      const frame = {
        project_id: pid, run_id: workflow.run_id, kind: workflow.kind, done: 0, total: 5,
        label: "progress.stage.prescan", payload: {}, updated_at: new Date().toISOString(),
        elapsed_seconds: 0,
      };
      await page.route(`**/projects/${pid}/workflow`, route =>
        route.fulfill({ json: { ...workflow, status: "running", progress: frame } }));
      let socket!: WebSocketRoute;
      await page.routeWebSocket(`**/ws/projects/${pid}/progress`, ws => ws.onMessage(() => {
        socket = ws;
        ws.send(JSON.stringify({ kind: "snapshot", project: { id: pid } }));
      }));
      await page.goto(`/projects/${pid}`);
      const status = page.getByRole("region", { name: t("workflowPanel.currentWorkflow") }).getByRole("status");
      await expect(status).toContainText(locale === "en" ? "Prescanning chapters…" : "正在预扫章节…");
      await expect.poll(() => Boolean(socket)).toBe(true);
      for (const [label, en, zh] of [
        ["progress.stage.chapter:3", "Chapter 3", "第 3 章"],
        ["progress.stage.reviewChapters", "Loading chapters for review…", "正在载入待审校章节…"],
        ["progress.stage.reviewRound:1", "Review round 1", "第 1 轮审校"],
        ["progress.stage.futureStage:2", "Unknown stage", "未知阶段"],
        ["progress.stage.reviewRound:2", "Review round 2", "第 2 轮审校"],
        // Older engines used a separate wire label for the first round.
        ["progress.stage.reviewRound1", "Review round 1", "第 1 轮审校"],
        ["A chapter: its title", "A chapter: its title", "A chapter: its title"],
      ]) {
        socket.send(JSON.stringify({ ...frame, label, done: 1, updated_at: new Date().toISOString() }));
        await expect(status).toContainText(locale === "en" ? en : zh);
        await expect(status).toContainText(t("workflow.stageCount", { done: 1, total: frame.total }));
      }
    });

    test("shows the current stage directly without reports or nested workflow panels", async ({
      page,
    }) => {
      const apiBase = apiOrigin ?? "**/api";
      const reportRequests: string[] = [];
      page.on("request", (request) => {
        if (new URL(request.url()).pathname.endsWith(`/projects/${pid}/report`))
          reportRequests.push(request.url());
      });
      await page.route(`${apiBase}/projects/${pid}`, (route) =>
        route.fulfill({ json: { ...project, status: "translating" } }),
      );
      await page.route(`**/projects/${pid}/workflow`, (route) =>
        route.fulfill({
          json: {
            ...workflow,
            status: "running",
            progress: {
              kind: "translation", project_id: pid, run_id: "run-a",
              label: stage, done: 2, total: 5,
            },
          },
        }),
      );
      await page.goto(`/projects/${pid}`);
      const panel = page.getByRole("region", { name: t("workflowPanel.currentWorkflow") });
      await expect(panel).toBeVisible();
      await expect(panel.getByRole("status")).toContainText(stage);
      await expect(panel.getByRole("status")).toContainText(
        t("workflow.stageCount", { done: 2, total: 5 }),
      );
      await expect(panel.getByRole("status")).toContainText(t("common.running"));
      await expect(panel.locator("details, ol")).toHaveCount(0);
      await expect(page.getByText(/Latest stage:|最近步骤：/)).toHaveCount(0);
      await expect(page.getByText(/^(Project report|项目报告)$/)).toHaveCount(0);
      await expect(page.getByRole("button", { name: /Update report|更新报告/ })).toHaveCount(0);
      await expect(page.getByRole("region", { name: t("progress.totalUsageRunTime") }))
        .toBeVisible();
      await expect(page.getByRole("link", { name: t("contents.readContentsSource") }))
        .toBeVisible();
      await expect(page.getByRole("columnheader", { name: t("progress.translationStatus") }))
        .toBeVisible();
      expect(reportRequests).toEqual([]);
      // The workflow owns one group-card outline; its status stays an unboxed text row.
      await expect(panel).toHaveCSS("border-left-width", "1px");
      await expect(panel.getByRole("status")).toHaveCSS("border-left-width", "0px");
      for (const element of [panel, panel.getByRole("status")]) {
        await expect(element).toHaveCSS("box-shadow", "none");
      }
    });

    for (const [status, label] of [
      ["not_started", "workflowPanel.notStarted"],
      ["queued", "common.queued"],
      ["done", "common.completed"],
    ] as const) {
      test(`${status} shows its state without stale progress or a waiting message`, async ({
        page,
      }) => {
        await page.route(`**/projects/${pid}/workflow`, (route) =>
          route.fulfill({
            json: {
              ...workflow,
              status,
              progress: {
                kind: "translation", project_id: pid, run_id: "run-a",
                label: stage, done: 1, total: 1,
              },
            },
          }),
        );
        await page.goto(`/projects/${pid}`);
        const panel = page.getByRole("region", { name: t("workflowPanel.currentWorkflow") });
        await expect(panel.getByRole("status")).toContainText(t(label));
        await expect(panel).not.toContainText(stage);
        await expect(panel).not.toContainText(t("workflow.stageCount", { done: 1, total: 1 }));
        await expect(panel).not.toContainText(t("workflowPanel.waitingForProgressUpdates"));
      });
    }

    for (const [status, label] of [
      ["paused", "common.paused"],
      ["error", "common.failed"],
    ] as const) {
      test(`${status} keeps the saved stage after reload`, async ({ page }) => {
        const apiBase = apiOrigin ?? "**/api";
        await page.route(`${apiBase}/projects/${pid}`, (route) =>
          route.fulfill({ json: { ...project, status } }),
        );
        await page.route(`**/projects/${pid}/workflow`, (route) =>
          route.fulfill({
            json: {
              ...workflow,
              status,
              progress: {
                kind: "translation", project_id: pid, run_id: "run-a",
                label: stage, done: 2, total: 5,
              },
            },
          }),
        );
        await page.goto(`/projects/${pid}`);
        const panel = page.getByRole("region", { name: t("workflowPanel.currentWorkflow") });
        await expect(panel.getByRole("status")).toContainText(t(label));
        await expect(panel.getByRole("status")).toContainText(stage);
        await page.reload();
        await expect(panel.getByRole("status")).toContainText(stage);
        await expect(page.getByRole("button", { name: t("progress.resumeTask") }))
          .toBeVisible();
      });
    }

    test("a running task without telemetry waits without inventing a stage", async ({ page }) => {
      await page.route(`**/projects/${pid}/workflow`, (route) =>
        route.fulfill({ json: { ...workflow, status: "running", progress: null } }),
      );
      await page.goto(`/projects/${pid}`);
      const panel = page.getByRole("region", { name: t("workflowPanel.currentWorkflow") });
      await expect(panel.getByRole("status"))
        .toContainText(t("workflowPanel.waitingForProgressUpdates"));
      await expect(panel.getByRole("status")).toContainText(t("common.running"));
    });

    test("long stage text wraps on narrow screens", async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      const longStage = `${stage} ${"chapter-with-a-long-name".repeat(12)}`;
      await page.route(`**/projects/${pid}/workflow`, (route) =>
        route.fulfill({
          json: {
            ...workflow,
            status: "running",
            progress: {
              kind: "translation", project_id: pid, run_id: "run-a",
              label: longStage, done: 2, total: 5,
            },
          },
        }),
      );
      await page.goto(`/projects/${pid}`);
      const panel = page.getByRole("region", { name: t("workflowPanel.currentWorkflow") });
      await expect(panel.getByRole("status")).toContainText(longStage);
      await expect(panel.getByRole("status"))
        .toContainText(t("workflow.stageCount", { done: 2, total: 5 }));
      expect(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth))
        .toBe(true);
      expect(await page.evaluate(() =>
        document.documentElement.scrollWidth <= window.innerWidth,
      )).toBe(true);
    });
  });
}
}
