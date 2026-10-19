import { expect, test } from "@playwright/test";
import { createTranslator } from "../../../packages/ui/src/i18n/catalog";
import { fakeApi, pid } from "./fixtures";

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
      ];
      for (const [path, title] of routes) {
        await page.goto(path);
        const header = page.locator('[data-slot="page.header"]');
        await expect(header.getByRole("heading", { name: title, exact: true }))
          .toBeVisible();
        await expect(header.locator("p")).toHaveCount(0);
      }
      const manageModels = page.getByRole("link", {
        name: t("settings.manageGlobalModels"),
        exact: true,
      });
      await expect(manageModels).toHaveAttribute("href", "/settings");
      await expect(manageModels.locator(".."))
        .toHaveText(t("settings.manageGlobalModels"));
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
      await expect(creation.getByText(t("createProject.prepareHelp"), { exact: true }))
        .toBeVisible();

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
