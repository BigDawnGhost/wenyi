import { expect, test } from "@playwright/test";
import { registerTests } from "../../../../packages/ui/tests/e2e/standalone-settings";
import { expectCardSurface } from "../../../../packages/ui/tests/e2e/color-helper";
import { fakeApi } from "../../../../packages/ui/sandbox/fixtures";
import { registerDesktopSetup } from "./shared-setup";

registerDesktopSetup();
registerTests({
  manualKeys: true,
  categories: {
    en: [
      "General", "Models & connections", "Stage models", "Workflow", "PDF",
      "Advanced", "Diagnostics and feedback", "About",
    ],
    "zh-CN": [
      "通用", "模型与接口", "各阶段模型", "翻译流程", "PDF",
      "高级", "诊断与反馈", "关于",
    ],
  },
});

for (const chinese of [false, true]) {
  test(`PDF settings combine MinerU and BabelDOC with one navigation entry in ${chinese ? "Chinese" : "English"}`, async ({ page }, testInfo) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
    await fakeApi(page, {
      "/desktop/plugins/babeldoc": {
        enabled: false, installed: false, supported: true, phase: "disabled",
        downloaded: 0, total: null, error: null, version: null,
      },
    });
    await page.goto("/settings/pdf");
    await expect(page.getByRole("heading", { name: "PDF", exact: true, level: 1 })).toBeVisible();
    const navigation = page.getByRole("navigation", { name: chinese ? "设置导航" : "Settings navigation", exact: true });
    await expect(navigation.getByRole("link", { name: "PDF", exact: true })).toHaveCount(1);
    await expect(navigation.getByRole("link", { name: /MinerU|BabelDOC/ })).toHaveCount(0);
    const mineru = page.getByTestId("mineru-settings");
    const babeldoc = page.getByTestId("babeldoc-plugin");
    await expect(mineru.getByLabel(chinese ? "MinerU API 密钥" : "MinerU API key", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: chinese ? "BabelDOC 插件" : "BabelDOC plugin", exact: true })).toBeVisible();
    await expect(babeldoc.getByRole("button", { name: chinese ? "启用并下载" : "Enable and download", exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`pdf-settings-${chinese ? "zh" : "en"}.png`), fullPage: true });
    for (const legacy of ["mineru", "plugins"]) {
      await page.goto(`/settings/${legacy}`);
      await expect(page).toHaveURL("/settings/pdf");
      await expect(mineru).toBeVisible();
      await expect(babeldoc).toBeVisible();
    }
  });
}

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
      await page.goto("/settings/pdf");
      await expectCardSurface(page.getByLabel(chinese ? "MinerU API 密钥" : "MinerU API key", { exact: true }));
      await page.screenshot({ path: testInfo.outputPath(`pdf-${chinese ? "zh" : "en"}-${theme}.png`) });
    });
  }
}
