import { expect, test } from "@playwright/test";
import { fakeApi } from "./fixtures";

test("plugin setup persists across navigation, retries and shares update progress style", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    localStorage.setItem("wenyi.locale", "zh-CN");
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "fixture" },
      __TAURI_INTERNALS__: {
        invoke: async () => ({
          currentVersion: "1.0.0", mode: "automatic", phase: "downloading",
          downloaded: 32 * 1024 * 1024, total: 128 * 1024 * 1024,
        }),
      },
    });
  });
  await fakeApi(page);
  const state = {
    enabled: false, installed: false, supported: true, phase: "disabled",
    downloaded: 0, total: null as number | null, error: null as string | null, version: "0.1.0",
  };
  const actions: string[] = [];
  await page.route("**/api/desktop/plugins/babeldoc{,/**}", async route => {
    const operation = route.request().url().split("/").pop()!;
    if (route.request().method() === "POST") {
      actions.push(operation);
      state.enabled = operation === "enable";
      state.phase = operation === "enable" ? "runtime" : "disabled";
      state.error = null;
    }
    await route.fulfill({ json: state });
  });
  await page.goto("/settings");
  const plugin = page.getByTestId("babeldoc-plugin");
  await expect(plugin).toContainText("未启用");
  await expect(plugin).toContainText("AGPL-3.0-only");
  await expect(plugin).not.toContainText("可选的本地 PDF");
  await expect(plugin).not.toContainText("启用后从");
  expect(actions).toEqual([]);
  await plugin.getByRole("button", { name: "启用并下载" }).click();
  await expect(plugin.getByRole("progressbar")).toBeVisible();
  state.phase = "models";
  state.downloaded = 256 * 1024 * 1024;
  state.total = 512 * 1024 * 1024;
  await expect(plugin).toContainText("正在下载模型和字体");
  await expect(plugin).toContainText("256.0 MB / 512.0 MB");
  await expect(plugin.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  await expect(plugin.getByRole("button")).toHaveCount(1);
  const cancel = plugin.getByRole("button", { name: "取消下载" });
  await expect(cancel).toBeVisible();
  const buttonBox = await cancel.boundingBox();
  const progressBox = await plugin.locator('[data-slot="transfer-progress"]').boundingBox();
  expect(buttonBox!.y + buttonBox!.height).toBeLessThan(progressBox!.y);
  expect(Math.abs(buttonBox!.x + buttonBox!.width - progressBox!.x - progressBox!.width)).toBeLessThanOrEqual(1);
  await plugin.screenshot({ path: testInfo.outputPath("plugin-progress.png") });
  const update = page.getByTestId("desktop-updates");
  expect(await plugin.locator('[data-slot="transfer-progress"]').getAttribute("class"))
    .toEqual(await update.locator('[data-slot="transfer-progress"]').getAttribute("class"));
  await page.getByRole("link", { name: "项目列表", exact: true }).click();
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await expect(plugin.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  state.phase = "error";
  state.error = "install_failed";
  await expect(plugin.getByRole("alert")).toContainText("插件安装失败");
  await plugin.getByRole("button", { name: "重试", exact: true }).click();
  await expect(plugin).toContainText("正在下载插件运行环境");
  await plugin.getByRole("button", { name: "取消下载" }).click();
  await expect(plugin).toContainText("未启用");
  expect(actions).toEqual(["enable", "enable", "disable"]);
});

test("ready plugin can be disabled but busy jobs keep it enabled with feedback", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "fixture" },
  }));
  await fakeApi(page);
  await page.route("**/api/desktop/plugins/babeldoc", route => route.fulfill({
    json: { enabled: true, installed: true, supported: true, phase: "ready", downloaded: 0, total: null, error: null, version: "0.1.0" },
  }));
  await page.route("**/api/desktop/plugins/babeldoc/disable", route => route.fulfill({
    status: 409, json: { detail: "Desktop jobs are in progress" },
  }));
  await page.goto("/settings");
  const plugin = page.getByTestId("babeldoc-plugin");
  await plugin.getByRole("button", { name: "Disable plugin" }).click();
  await expect(plugin.getByRole("alert")).toContainText("Finish all project jobs");
  await expect(plugin.getByRole("status")).toContainText("Ready");
});
