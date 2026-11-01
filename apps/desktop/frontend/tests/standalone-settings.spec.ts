import { expect, test } from "@playwright/test";
import { registerTests } from "../../../../packages/ui/tests/e2e/standalone-settings";
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
  test(`About retains the version and update controls in ${chinese ? "Chinese" : "English"}`, async ({ page }) => {
    await page.addInitScript(locale => {
      localStorage.setItem("wenyi.locale", locale);
      Object.assign(window, {
        __TAURI_INTERNALS__: {
          invoke: async (command: string) => command === "desktop_update_status" ? {
            currentVersion: "1.0.0", mode: "automatic", phase: "current",
            downloaded: 0, total: 0,
          } : null,
        },
      });
    }, chinese ? "zh-CN" : "en");
    await page.goto("/settings");
    await page.getByRole("link", { name: chinese ? "关于" : "About", exact: true }).click();
    await expect(page).toHaveURL("/settings/updates");
    await expect(page.getByRole("heading", { name: chinese ? "关于" : "About", level: 1 })).toBeVisible();
    await expect(page.getByText(chinese ? "当前版本：1.0.0" : "Current version: 1.0.0", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: chinese ? "检查更新" : "Check for updates", exact: true })).toBeEnabled();
  });
}
