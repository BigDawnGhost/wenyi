import { expect, test } from "@playwright/test";
import { fakeApi, globalConfiguration } from "./fixtures";

async function setup(page: import("@playwright/test").Page, mode = "automatic", phase = "available") {
  await page.addInitScript(({ mode, phase }) => {
    const status = {
      currentVersion: "1.0.0", mode, phase, version: "1.1.0",
      notes: "<b>Plain text release notes</b>", downloaded: 0, total: 100,
      error: undefined as string | undefined,
    };
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
      __TAURI_INTERNALS__: {
        invoke: async (command: string) => {
          if (command === "desktop_update_status") return { ...status };
          if (command === "desktop_update_check") status.phase = "available";
          if (command === "desktop_update_download") {
            status.phase = "downloading";
            status.downloaded = 50;
            setTimeout(() => { status.phase = "ready"; status.downloaded = 100; }, 1500);
          }
          if (command === "desktop_update_install") { status.error = "busy"; status.phase = "ready"; throw "busy"; }
        },
      },
    });
  }, { mode, phase });
  await fakeApi(page);
  await page.goto("/settings");
}

test("updates sit last, show literal notes, poll download and confirm installation with busy feedback", async ({ page }) => {
  await setup(page);
  const section = page.getByTestId("desktop-updates");
  await expect(section).toContainText("Current version: 1.0.0");
  await expect(section).toContainText("<b>Plain text release notes</b>");
  expect(await section.evaluate(el => el.nextElementSibling === null)).toBe(true);
  await section.getByRole("button", { name: "Check for updates" }).click();
  await section.getByRole("button", { name: "Download update" }).click();
  await expect(section.getByRole("progressbar")).toHaveAttribute("value", "50");
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await expect(section).toHaveCount(0);
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await section.getByRole("button", { name: "Install and restart", exact: true }).click();
  await expect(section.getByRole("alertdialog")).toBeVisible();
  await section.getByRole("button", { name: "Confirm install and restart" }).click();
  await expect(section.getByRole("alert")).toContainText("all project jobs and native exports");
});

for (const mode of ["manual", "unconfigured"]) {
  test(`${mode} offers release fallback without native download/install`, async ({ page }) => {
    await setup(page, mode);
    const section = page.getByTestId("desktop-updates");
    await expect(section).toContainText("Automatic installation is unavailable");
    await expect(section.getByRole("button", { name: "Open releases" })).toBeVisible();
    await expect(section.getByRole("button", { name: "Download update" })).toHaveCount(0);
    await expect(section.getByRole("button", { name: "Install and restart" })).toHaveCount(0);
  });
}

test("validated YAML is still unsaved and prevents installation", async ({ page }) => {
  await setup(page, "automatic", "ready");
  await page.locator("summary").filter({ hasText: "Advanced YAML configuration" }).click();
  const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration" });
  await yaml.fill((await yaml.inputValue()) + "\n# changed");
  await expect(page.getByRole("button", { name: "Install and restart", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Validate configuration", exact: true }).click();
  await expect(page.getByRole("button", { name: "Install and restart", exact: true })).toBeDisabled();
});

test("Chinese update labels remain Desktop-only", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await setup(page);
  await expect(page.getByTestId("desktop-updates")).toContainText("桌面端更新");
  await expect(page.getByRole("button", { name: "检查更新", exact: true })).toBeVisible();
});

test("unsaved and pending credential inputs block installation", async ({ page }) => {
  await setup(page, "automatic", "ready");
  const connection = Object.keys(globalConfiguration.effective.llm.providers)[0];
  const status = { mode: "manual", storage: "session", available: true,
    requires_key: true, system_storage_available: false, environment: "FAKE_KEY" };
  await page.route("**/api/desktop/credentials", route => route.fulfill({ json: { [connection]: status } }));
  let finish!: () => void;
  const saved = new Promise<void>(resolve => { finish = resolve; });
  await page.route(`**/api/desktop/credentials/${connection}`, async route => {
    await saved;
    await route.fulfill({ json: status });
  });
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  const install = page.getByRole("button", { name: "Install and restart", exact: true });
  await page.getByLabel("API key", { exact: true }).fill("fake-test-value");
  await expect(install).toBeDisabled();
  await page.getByRole("button", { name: "Save key", exact: true }).click();
  await expect(install).toBeDisabled();
  finish();
  await expect(install).toBeEnabled();
});
