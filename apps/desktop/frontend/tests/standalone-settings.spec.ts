import { expect, test } from "@playwright/test";
import { registerTests } from "../../../../packages/ui/tests/e2e/standalone-settings";
import { expectCardSurface } from "../../../../packages/ui/tests/e2e/color-helper";
import { registerDesktopSetup } from "./shared-setup";

registerDesktopSetup();
registerTests({
  categories: {
    en: [
      "General", "Models & connections", "Default models", "Workflow", "PDF parsing",
      "BabelDOC plugin", "Advanced", "Diagnostics and feedback", "About",
    ],
    "zh-CN": [
      "通用", "模型与接口", "默认模型", "翻译流程", "PDF 解析",
      "BabelDOC 插件", "高级", "诊断与反馈", "关于",
    ],
  },
});

for (const chinese of [false, true]) {
  for (const theme of ["light", "dark"]) {
    test(`About and PDF parsing use panels in ${chinese ? "Chinese" : "English"} in ${theme}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.addInitScript(({ locale, theme }) => {
        localStorage.setItem("wenyi.locale", locale);
        localStorage.setItem("wenyi.theme", theme);
        Object.assign(window, {
          __TAURI_INTERNALS__: {
            invoke: async (command: string) => command === "desktop_update_status" ? {
              currentVersion: "1.0.0", mode: "manual", phase: "idle",
              downloaded: 0, total: 0,
            } : null,
          },
        });
      }, { locale: chinese ? "zh-CN" : "en", theme });
      await page.goto("/settings");
      await page.getByRole("link", { name: chinese ? "关于" : "About", exact: true }).click();
      await expect(page).toHaveURL("/settings/updates");
      await expect(page.getByRole("heading", { name: chinese ? "关于" : "About", level: 1 })).toBeVisible();
      await expectCardSurface(page.getByText(chinese ? "当前版本：1.0.0" : "Current version: 1.0.0", { exact: true }));
      await expect(page.getByRole("button", { name: chinese ? "检查更新" : "Check for updates", exact: true })).toBeEnabled();
      await page.screenshot({ path: testInfo.outputPath(`about-${chinese ? "zh" : "en"}-${theme}.png`) });
      await page.goto("/settings/mineru");
      await expectCardSurface(page.getByLabel(chinese ? "MinerU API 密钥" : "MinerU API key", { exact: true }));
      await page.screenshot({ path: testInfo.outputPath(`pdf-${chinese ? "zh" : "en"}-${theme}.png`) });
    });
  }
}
