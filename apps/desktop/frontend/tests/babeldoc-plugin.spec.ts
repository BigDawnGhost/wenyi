import { expect, test } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/tests/e2e/fixtures";

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
  await page.goto("/settings/plugins");
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
  await page.getByRole("button", { name: "返回", exact: true }).click();
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await page.getByRole("link", { name: "BabelDOC 插件", exact: true }).click();
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
  await page.goto("/settings/plugins");
  const plugin = page.getByTestId("babeldoc-plugin");
  await plugin.getByRole("button", { name: "Disable plugin" }).click();
  await expect(plugin.getByRole("alert")).toContainText("Finish all project jobs");
  await expect(plugin.getByRole("status")).toContainText("Ready");
});

test("plugin upgrades are manual, confirmed, and keep the active version until ready", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    localStorage.setItem("wenyi.locale", "zh-CN");
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "fixture" },
    });
  });
  await fakeApi(page);
  const state = {
    enabled: true, installed: true, supported: true, phase: "ready",
    downloaded: 0, total: null as number | null, version: "0.1.0", error: null,
    latest_version: null as string | null, checking_update: false,
    update_available: false, updating: false, update_error: null as string | null,
  };
  const actions: string[] = [];
  await page.route("**/api/desktop/plugins/babeldoc{,/**}", async route => {
    if (route.request().method() === "POST") {
      const operation = route.request().url().split("/").pop()!;
      actions.push(operation);
      if (operation === "check-update") {
        state.checking_update = true;
      } else if (operation === "update") {
        state.updating = true;
        state.phase = "runtime";
        state.update_error = null;
      }
    }
    await route.fulfill({ json: state });
  });
  await page.goto("/settings/plugins");
  const plugin = page.getByTestId("babeldoc-plugin");
  await expect(plugin).toContainText("插件版本：0.1.0");
  expect(actions).toEqual([]);
  await expect(plugin.getByRole("button", { name: "更新插件", exact: true })).toHaveCount(0);
  await plugin.getByRole("button", { name: "检查插件更新" }).click();
  await expect(plugin.getByRole("button", { name: "正在检查插件更新" })).toBeDisabled();
  state.checking_update = false;
  state.latest_version = "0.1.1";
  state.update_available = true;
  await expect(plugin).toContainText("可用插件版本：0.1.1");
  await plugin.getByRole("button", { name: "更新插件", exact: true }).click();
  const confirmation = plugin.getByRole("alertdialog");
  await expect(confirmation).toContainText("需要重新解析");
  await expect(confirmation).toContainText("项目数据不会被删除");
  await confirmation.getByRole("button", { name: "取消", exact: true }).click();
  expect(actions).toEqual(["check-update"]);
  await plugin.getByRole("button", { name: "更新插件", exact: true }).click();
  await confirmation.getByRole("button", { name: "确认更新插件" }).click();
  await expect(plugin.getByRole("button", { name: "取消更新" })).toBeVisible();
  await expect(plugin).toContainText("插件版本：0.1.0");
  state.downloaded = 32 * 1024 * 1024;
  state.total = 64 * 1024 * 1024;
  await expect(plugin.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  await plugin.screenshot({ path: testInfo.outputPath("plugin-upgrade-progress.png") });
  await page.getByRole("button", { name: "返回", exact: true }).click();
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await page.getByRole("link", { name: "BabelDOC 插件", exact: true }).click();
  await expect(plugin.getByRole("button", { name: "取消更新" })).toBeVisible();
  state.phase = "ready";
  state.updating = false;
  state.update_error = "update_busy";
  await expect(plugin.getByRole("alert")).toContainText("未切换版本");
  await expect(plugin).toContainText("插件版本：0.1.0");
  await plugin.getByRole("button", { name: "更新插件", exact: true }).click();
  await confirmation.getByRole("button", { name: "确认更新插件" }).click();
  await expect(plugin.getByRole("button", { name: "取消更新" })).toBeVisible();
  state.phase = "ready";
  state.updating = false;
  state.version = "0.1.1";
  state.update_available = false;
  await expect(plugin).toContainText("插件版本：0.1.1");
  await expect(plugin).toContainText("插件已是最新版本");
  await expect(plugin.getByRole("button", { name: "更新插件", exact: true })).toHaveCount(0);
  expect(actions).toEqual(["check-update", "update", "update"]);
});

test("failed checks and cancelled upgrades leave the installed plugin available", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "fixture" },
  }));
  await fakeApi(page);
  const state = {
    enabled: true, installed: true, supported: true, phase: "ready",
    downloaded: 0, total: null, version: "0.1.0", error: null,
    latest_version: null as string | null, checking_update: false,
    update_available: false, updating: false, update_error: null as string | null,
  };
  let checks = 0;
  let cancelled = false;
  await page.route("**/api/desktop/plugins/babeldoc{,/**}", async route => {
    if (route.request().method() === "POST") {
      const operation = route.request().url().split("/").pop();
      if (operation === "check-update") {
        state.update_error = checks++ === 0 ? "check_failed" : null;
        state.latest_version = state.update_error ? null : "0.1.1";
        state.update_available = !state.update_error;
      } else if (operation === "update") {
        state.updating = true;
        state.phase = "models";
      } else if (operation === "cancel-update") {
        cancelled = true;
        state.updating = false;
        state.phase = "ready";
      }
    }
    await route.fulfill({ json: state });
  });
  await page.goto("/settings/plugins");
  const plugin = page.getByTestId("babeldoc-plugin");
  await plugin.getByRole("button", { name: "Check for plugin updates" }).click();
  await expect(plugin.getByRole("alert")).toContainText("current installation is unchanged");
  await expect(plugin).toContainText("Ready");
  await plugin.getByRole("button", { name: "Check for plugin updates" }).click();
  await plugin.getByRole("button", { name: "Update plugin", exact: true }).click();
  await plugin.getByRole("button", { name: "Confirm plugin update" }).click();
  await plugin.getByRole("button", { name: "Cancel update" }).click();
  await expect(plugin).toContainText("Ready");
  await expect(plugin).toContainText("Plugin version: 0.1.0");
  await expect(plugin.getByRole("progressbar")).toHaveCount(0);
  expect(cancelled).toBe(true);
});

test("a disabled installed plugin can be updated without enabling it", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "fixture" },
  }));
  await fakeApi(page);
  const state = {
    enabled: false, installed: true, supported: true, phase: "disabled",
    downloaded: 0, total: null, version: "0.1.0", error: null,
    latest_version: null as string | null, checking_update: false,
    update_available: false, updating: false, update_error: null,
  };
  const actions: string[] = [];
  await page.route("**/api/desktop/plugins/babeldoc{,/**}", async route => {
    if (route.request().method() === "POST") {
      const operation = route.request().url().split("/").pop()!;
      actions.push(operation);
      if (operation === "check-update") {
        state.latest_version = "0.1.1";
        state.update_available = true;
      } else if (operation === "update") {
        state.version = "0.1.1";
        state.update_available = false;
      }
    }
    await route.fulfill({ json: state });
  });
  await page.goto("/settings/plugins");
  const plugin = page.getByTestId("babeldoc-plugin");
  await plugin.getByRole("button", { name: "Check for plugin updates" }).click();
  await plugin.getByRole("button", { name: "Update plugin", exact: true }).click();
  await plugin.getByRole("button", { name: "Confirm plugin update" }).click();
  await expect(plugin).toContainText("Plugin version: 0.1.1");
  await expect(plugin).toContainText("Disabled");
  await expect(plugin.getByRole("button", { name: "Enable plugin" })).toBeVisible();
  await expect(plugin.getByRole("button", { name: "Disable plugin" })).toHaveCount(0);
  expect(actions).toEqual(["check-update", "update"]);
});
