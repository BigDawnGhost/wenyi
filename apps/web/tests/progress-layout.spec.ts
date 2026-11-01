import { expect, test } from "@playwright/test";
import { createTranslator } from "../../../packages/ui/src/i18n/catalog";
import { fakeApi, pid, project, workflow } from "./fixtures";

for (const locale of ["en", "zh-CN"] as const) {
  const t = createTranslator(locale);
  const stage = locale === "en" ? "Translating chapter 3" : "正在翻译第 3 章";

  test.describe(`flat translation progress in ${locale}`, () => {
    test.beforeEach(async ({ page }, info) => {
      const origin = info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined;
      await page.addInitScript(({ locale, origin }) => {
        localStorage.setItem("wenyi.locale", locale);
        if (origin) {
          Object.assign(window, {
            __WENYI_DESKTOP__: { apiBase: origin, token: "progress-layout-fixture" },
          });
        }
      }, { locale, origin });
      await fakeApi(page, {}, origin);
    });

    test("shows the current stage directly without reports or nested workflow panels", async ({
      page,
    }, info) => {
      const apiBase = info.project.name === "desktop" ? "http://127.0.0.1:19485" : "**/api";
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
      // The workflow and its status are text sections, not bordered containers.
      for (const element of [panel, panel.getByRole("status")]) {
        await expect(element).toHaveCSS("border-left-width", "0px");
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
      test(`${status} keeps the saved stage after reload`, async ({ page }, info) => {
        const apiBase = info.project.name === "desktop" ? "http://127.0.0.1:19485" : "**/api";
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
