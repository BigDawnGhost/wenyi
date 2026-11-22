import { expect, test, type Page } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/sandbox/fixtures";

const origin = "http://127.0.0.1:19481";

async function setup(page: Page, permission = "not_determined", answer = "granted", failed = false, locale = "en") {
  await page.addInitScript(({ origin, permission, answer, failed, locale }) => {
    localStorage.setItem("wenyi.locale", locale);
    const requests: boolean[] = [];
    Object.assign(window, {
      __WENYI_DESKTOP__: failed ? undefined : { apiBase: origin, token: "fake-private-token" },
      __WENYI_DESKTOP_ERROR__: failed ? "Local engine failed." : undefined,
      notificationRequests: requests,
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args: Record<string, unknown>) => {
          if (command === "desktop_notifications_enabled") return true;
          if (command !== "desktop_notifications_permission") return null;
          requests.push(args.request === true);
          return args.request ? answer : permission;
        },
      },
    });
  }, { origin, permission, answer, failed, locale });
  if (!failed) await fakeApi(page, {}, origin);
}

function requests(page: Page) {
  return page.evaluate(() => (window as unknown as {
    notificationRequests: boolean[];
  }).notificationRequests);
}

test("permission is requested only after explicit consent", async ({ page }) => {
  await setup(page);
  await page.goto("/");
  const dialog = page.getByRole("dialog", { name: "Desktop status notifications" });
  await expect(dialog.getByText(/never book text or credentials/)).toBeVisible();
  expect((await requests(page)).every(request => !request)).toBe(true);
  await dialog.getByRole("button", { name: "Allow notifications" }).click();
  await expect(dialog).toHaveCount(0);
  expect((await requests(page)).filter(Boolean)).toEqual([true]);
});

test("not now does not open the OS permission prompt", async ({ page }) => {
  await setup(page);
  await page.goto("/");
  const dialog = page.getByRole("dialog", { name: "Desktop status notifications" });
  await dialog.getByRole("button", { name: "Not now" }).click();
  await expect(dialog).toHaveCount(0);
  expect((await requests(page)).filter(Boolean)).toEqual([]);
});

test("denied permission explains system settings without requesting again", async ({ page }) => {
  await setup(page, "not_determined", "denied");
  await page.goto("/");
  const dialog = page.getByRole("dialog", { name: "Desktop status notifications" });
  await dialog.getByRole("button", { name: "Allow notifications" }).click();
  await expect(dialog.getByRole("status")).toContainText("System Settings > Notifications");
  await expect(dialog.getByRole("button", { name: "Allow notifications" })).toHaveCount(0);
  await dialog.getByRole("button", { name: "Close", exact: true })
    .filter({ hasText: "Close" }).click();
  await expect(dialog).toHaveCount(0);
  expect((await requests(page)).filter(Boolean)).toEqual([true]);
});

test("Chinese permission guidance uses the installed application's name", async ({ page }) => {
  await setup(page, "not_determined", "denied", false, "zh-CN");
  await page.goto("/");
  const dialog = page.getByRole("dialog", { name: "桌面状态通知" });
  await dialog.getByRole("button", { name: "允许通知" }).click();
  await expect(dialog.getByRole("status")).toContainText("Wenyi Desktop");
});

test("permission errors allow retry without exposing native errors", async ({ page }) => {
  await setup(page, "not_determined", "unavailable");
  await page.goto("/");
  const dialog = page.getByRole("dialog", { name: "Desktop status notifications" });
  await dialog.getByRole("button", { name: "Allow notifications" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Could not request notification permission");
  await expect(dialog.getByRole("button", { name: "Allow notifications" })).toBeEnabled();
});

for (const permission of ["not_required", "granted", "denied", "unavailable"]) {
  test(`${permission} does not interrupt startup`, async ({ page }) => {
    await setup(page, permission);
    await page.goto("/");
    await expect.poll(() => requests(page).then(values => values.length)).toBeGreaterThan(0);
    await expect(page.getByRole("dialog", { name: "Desktop status notifications" })).toHaveCount(0);
    expect((await requests(page)).filter(Boolean)).toEqual([]);
  });
}

test("engine startup failure never requests notification permission", async ({ page }) => {
  await setup(page, "not_determined", "granted", true);
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Local engine failed");
  expect(await requests(page)).toEqual([]);
});
