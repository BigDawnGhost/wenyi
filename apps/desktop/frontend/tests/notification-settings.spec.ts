import { expect, test, type Page } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/sandbox/fixtures";

const origin = "http://127.0.0.1:19481";

async function setup(page: Page, enabled = true, locale = "en", saveFails = false) {
  await page.addInitScript(({ origin, enabled, locale, saveFails }) => {
    localStorage.setItem("wenyi.locale", locale);
    if (sessionStorage.getItem("mock.notifications.enabled") === null) {
      sessionStorage.setItem("mock.notifications.enabled", String(enabled));
    }
    const calls: { command: string; args?: Record<string, unknown> }[] = [];
    Object.assign(window, {
      notificationCalls: calls,
      notificationSaveFails: saveFails,
      notificationReadFails: false,
      __WENYI_DESKTOP__: { apiBase: origin, token: "fake-private-token" },
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args?: Record<string, unknown>) => {
          calls.push({ command, args });
          if (command === "desktop_notifications_enabled") {
            if ((window as unknown as { notificationReadFails: boolean }).notificationReadFails) {
              throw { message: "private-native-error" };
            }
            return sessionStorage.getItem("mock.notifications.enabled") === "true";
          }
          if (command === "desktop_notifications_set_enabled") {
            if ((window as unknown as { notificationSaveFails: boolean }).notificationSaveFails) {
              throw { message: "private-native-error" };
            }
            sessionStorage.setItem("mock.notifications.enabled", String(args?.enabled));
            return args?.enabled;
          }
          if (command === "desktop_notifications_permission") return "not_determined";
          return null;
        },
      },
    });
  }, { origin, enabled, locale, saveFails });
  await fakeApi(page, {}, origin);
}

for (const [locale, label] of [["en", "Status notifications"], ["zh-CN", "状态通知"]]) {
  test(`General offers one immediate notification switch and restores it after reload in ${locale}`, async ({ page }) => {
    await setup(page, true, locale);
    const saves: string[] = [];
    page.on("request", request => {
      if (request.method() !== "GET" && request.url().startsWith(origin)) saves.push(request.url());
    });
    await page.goto("/settings");
    const prompt = page.getByRole("dialog", { name: /Desktop status notifications|桌面状态通知/ });
    await prompt.getByRole("button", { name: /Not now|暂不启用/ }).click();
    const control = page.getByRole("switch", { name: label, exact: true });
    await expect(control).toBeChecked();
    await control.click();
    await expect(control).not.toBeChecked();
    expect(saves).toEqual([]);
    await page.reload();
    await expect(control).not.toBeChecked();
    await expect(prompt).toHaveCount(0);
    await expect(page.getByRole("switch", { name: /Completion notifications|完成通知/ })).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.getItem("wenyi.notifications.enabled"))).toBeNull();
  });
}

test("disabled notifications never query or request OS permission at startup", async ({ page }) => {
  await setup(page, false);
  await page.goto("/settings");
  await expect(page.getByRole("switch", { name: "Status notifications", exact: true })).not.toBeChecked();
  expect(await page.evaluate(() => (window as unknown as {
    notificationCalls: { command: string }[];
  }).notificationCalls.filter(call => call.command === "desktop_notifications_permission"))).toEqual([]);
});

test("a failed save retains the confirmed value and can be retried without exposing native errors", async ({ page }) => {
  await setup(page, false, "en", true);
  await page.goto("/settings");
  const control = page.getByRole("switch", { name: "Status notifications", exact: true });
  await expect(control).toBeEnabled();
  await control.click();
  await expect(page.getByRole("alert")).toContainText("Could not save notification settings");
  await expect(control).not.toBeChecked();
  await expect(page.getByText("private-native-error")).toHaveCount(0);
  await page.evaluate(() => {
    (window as unknown as { notificationSaveFails: boolean }).notificationSaveFails = false;
  });
  await control.click();
  const prompt = page.getByRole("dialog", { name: "Desktop status notifications" });
  await expect(prompt).toBeVisible();
  await prompt.getByRole("button", { name: "Not now", exact: true }).click();
  await expect(control).toBeChecked();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as {
    notificationCalls: { command: string; args?: { request?: boolean } }[];
  }).notificationCalls.filter(call =>
    call.command === "desktop_notifications_permission" && call.args?.request,
  ))).toEqual([]);
});

test("a failed read disables the switch and offers a safe retry", async ({ page }) => {
  await setup(page, false);
  await page.addInitScript(() => {
    (window as unknown as { notificationReadFails: boolean }).notificationReadFails = true;
  });
  await page.goto("/settings");
  const control = page.getByRole("switch", { name: "Status notifications", exact: true });
  await expect(control).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText("Could not read notification settings");
  await expect(page.getByText("private-native-error")).toHaveCount(0);
  await page.evaluate(() => {
    (window as unknown as { notificationReadFails: boolean }).notificationReadFails = false;
  });
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(control).toBeEnabled();
  await expect(control).not.toBeChecked();
});
