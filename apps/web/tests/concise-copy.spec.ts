import { expect, test } from "@playwright/test";
import { createTranslator } from "../../../packages/ui/src/i18n/catalog";
import { chapter, fakeApi, pid, project, workflow } from "./fixtures";

for (const locale of ["en", "zh-CN"] as const) {
  const t = createTranslator(locale);

  test.describe(`concise interface copy in ${locale}`, () => {
    test.beforeEach(async ({ page }, info) => {
      const origin = info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined;
      await page.addInitScript(({ locale, origin }) => {
        localStorage.setItem("wenyi.locale", locale);
        if (origin) {
          Object.assign(window, {
            __WENYI_DESKTOP__: { apiBase: origin, token: "concise-copy-fixture" },
          });
        }
      }, { locale, origin });
      await fakeApi(page, {}, origin);
    });

    test("page headers omit redundant introductions and keep model management", async ({
      page,
    }) => {
      const routes = [
        ["/", t("dashboard.myProjects")],
        ["/settings", t("settings.title")],
        [`/projects/${pid}/glossary`, t("common.glossary")],
        [`/projects/${pid}/review`, t("common.wholeBookReview")],
        [`/projects/${pid}/events`, t("common.eventLog")],
        [`/projects/${pid}/settings`, t("common.projectSettings")],
        [`/projects/${pid}/export`, t("export.exportTranslation")],
        [`/projects/${pid}/style`, t("common.styleSynopsis")],
        [`/projects/${pid}/contents`, t("contents.title")],
        [`/projects/${pid}/proofreading`, t("progress.manualProofreading")],
      ];
      for (const [path, title] of routes) {
        await page.goto(path);
        const header = page.locator('[data-slot="page.header"]');
        await expect(header.getByRole("heading", { name: title, exact: true }))
          .toBeVisible();
        await expect(header.locator("p")).toHaveCount(0);
      }
      await page.goto(`/projects/${pid}/settings`);
      const manageModels = page.getByRole("link", {
        name: t("settings.manageGlobalModels"),
        exact: true,
      });
      await expect(manageModels).toHaveAttribute("href", "/settings");
      await expect(manageModels).toHaveText(t("settings.manageGlobalModels"));
    });

    test("workflow separates style analysis from translation-time term extraction", async ({
      page,
    }) => {
      await page.route(`**/projects/${pid}/workflow`, (route) =>
        route.fulfill({
          json: {
            ...workflow,
            stages: [
              { id: "style_analysis", label: "风格分析", enabled: true },
              { id: "term_extract", label: "实时术语提取", enabled: true },
            ],
          },
        }),
      );
      await page.goto(`/projects/${pid}`);
      await page.getByText(t("progress.workflowDetails"), { exact: true }).click();
      await expect(page.getByText(locale === "en" ? "Style analysis" : "风格分析", {
        exact: true,
      })).toBeVisible();
      await expect(page.getByText(t("workflow.terms"), { exact: true })).toBeVisible();
      await expect(page.getByText(/initial glossary|初始术语/i)).toHaveCount(0);
    });

    test("dialogs retain named controls without redundant descriptions", async ({
      page,
    }) => {
      await page.goto("/projects/new");
      const creation = page.getByRole("dialog", {
        name: t("common.createProject"),
        exact: true,
      });
      await expect(creation).toBeVisible();
      await expect(creation).not.toHaveAttribute("aria-describedby");
      await expect(creation.getByLabel(t("createProject.projectName")))
        .toBeVisible();
      await expect(creation.getByRole("checkbox", { name: t("createProject.prepareSource") }))
        .toBeVisible();
      // Options carry their own names; there is no explanatory prose under them.
      await expect(creation.locator("p")).toHaveCount(0);

      await page.goto(`/projects/${pid}/proofreading/0`);
      await page.getByTestId("translation-text").click({ button: "right" });
      await page.getByRole("menuitem", {
        name: t("review.editTranslation"),
        exact: true,
      }).click();
      const editor = page.getByRole("dialog", {
        name: t("proofreading.paragraphEditor"),
        exact: true,
      });
      await expect(editor).toBeVisible();
      await expect(editor.locator("header p")).toHaveCount(0);
      await expect(editor).not.toHaveAttribute("aria-describedby");
      await expect(editor.getByRole("tab", {
        name: t("proofreading.changeHistory"),
        exact: true,
      })).toBeVisible();
      await expect(editor.getByRole("button", {
        name: t("review.saveTranslation"),
        exact: true,
      })).toBeVisible();
    });

    test("export history keeps its retention warning when empty or populated", async ({
      page,
    }, info) => {
      let populated = false;
      await page.route(`**/projects/${pid}/exports`, (route) =>
        route.fulfill({
          json: populated
            ? Array.from({ length: 7 }, (_, index) => ({
                id: 7 - index,
                project_id: pid,
                format: "txt",
                status: "done",
                size: 100,
                created_at: "2026-09-17T12:00:00Z",
                options: {},
              }))
            : [],
        }),
      );
      const notice = page.getByText(
        locale === "zh-CN"
          ? "仅保留最近 5 份导出文件，请及时保存到其他位置。"
          : "Only the latest 5 exports are kept. Save files elsewhere to keep them.",
        { exact: true },
      );
      await page.goto(`/projects/${pid}/export`);
      await expect(page.getByText(t("export.noExportsYet"), { exact: true }))
        .toBeVisible();
      await expect(notice).toBeVisible();

      populated = true;
      await page.reload();
      await expect(notice).toBeVisible();
      await expect(page.getByRole("button", {
        name: t(info.project.name === "desktop" ? "export.saveAs" : "export.download"),
        exact: true,
      })).toHaveCount(5);
    });

    test("review explains its translation prerequisite until all chapters are done", async ({
      page,
    }) => {
      let translated = false;
      await page.route(`**/projects/${pid}`, (route) =>
        route.fulfill({ json: { ...project, status: "paused" } }),
      );
      await page.route(`**/projects/${pid}/workflow`, (route) =>
        route.fulfill({ json: { ...workflow, status: "paused" } }),
      );
      await page.route(`**/projects/${pid}/chapters`, (route) =>
        route.fulfill({
          json: [
            chapter,
            { ...chapter, index: 1, status: translated ? "done" : "pending" },
          ],
        }),
      );
      const notice = page.getByText(
        locale === "zh-CN"
          ? "完成全部章节翻译后可审校。"
          : "Complete all chapter translations before running review.",
        { exact: true },
      );
      const start = page.getByRole("button", {
        name: t("review.runWholeBookReview"),
        exact: true,
      });
      await page.goto(`/projects/${pid}/review`);
      await expect(page.getByText(t("review.noReviewYet"), { exact: true }))
        .toBeVisible();
      await expect(notice).toBeVisible();
      await expect(start).toHaveCount(0);

      translated = true;
      await page.reload();
      await expect(start).toBeVisible();
      await expect(notice).toHaveCount(0);
    });

    test("glossary import states its supported formats and required CSV headers", async ({
      page,
    }) => {
      await page.goto(`/projects/${pid}/glossary`);
      await page.getByRole("button", {
        name: t("glossary.import"),
        exact: true,
      }).click();
      const dialog = page.getByRole("dialog", {
        name: t("glossary.importTerms"),
        exact: true,
      });
      await expect(dialog.getByText(
        locale === "zh-CN"
          ? "CSV / JSON；CSV 需包含 source、target 列头。"
          : "CSV / JSON; CSV requires source and target headers.",
        { exact: true },
      )).toBeVisible();
      await expect(dialog.locator('input[type="file"]'))
        .toHaveAttribute("accept", ".csv,.json");
      await expect(dialog.getByRole("button", {
        name: t("glossary.chooseFile"),
        exact: true,
      })).toBeVisible();
    });

    test("YAML summaries omit static help but retain unsaved changes", async ({
      page,
    }) => {
      for (const path of ["/settings", `/projects/${pid}/settings`]) {
        await page.goto(path);
        const summary = page.locator("summary").filter({
          hasText: t("settings.advancedYamlConfiguration"),
        });
        await expect(summary).toHaveText(t("settings.advancedYamlConfiguration"));
        await summary.click();
        await page.getByRole("textbox", {
          name: t("settings.advancedYamlConfiguration"),
          exact: true,
        }).fill("{}");
        await summary.click();
        await expect(summary).toContainText(t("settings.unsavedSummary"));
        await expect(page.getByRole("button", {
          name: t("settings.validateConfiguration"),
          exact: true,
        })).toBeEnabled();
      }
    });
  });
}
