import { expect, test, type Page } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/sandbox/fixtures";

const origin = "http://127.0.0.1:19481";

async function setup(page: Page, supported = true, locale = "en") {
  await page.addInitScript(({ origin, supported, locale }) => {
    localStorage.setItem("wenyi.locale", locale);
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: origin, token: "fake-private-token" },
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args?: { percent?: number }) => {
          if (command === "desktop_notifications_enabled") return false;
          if (command !== "desktop_interface_zoom" || !supported) return null;
          if (args?.percent !== undefined) {
            if (sessionStorage.getItem("mock.zoom.fails")) throw new Error("Native details");
            sessionStorage.setItem("mock.zoom.saved", String(args.percent));
          } else if (sessionStorage.getItem("mock.zoom.readFails")) {
            throw new Error("Native details");
          }
          return Number(sessionStorage.getItem("mock.zoom.saved") ?? 100);
        },
      },
    });
  }, { origin, supported, locale });
  await fakeApi(page, {}, origin);
  await page.goto("/settings");
}

test("applies zoom presets, restores the selected preset, and can reset from another page", async ({ page }) => {
  await setup(page);
  const zoom = page.getByRole("combobox", { name: "Interface zoom" });
  const list = page.getByRole("listbox");
  await expect(zoom).toHaveText("100%");
  await zoom.press("Enter");
  await expect(list.getByRole("option")).toHaveText(["80%", "90%", "100%", "110%", "125%"]);
  await expect(list.getByRole("option", { name: "100%", exact: true })).toBeFocused();
  await page.keyboard.press("End");
  await expect(list.getByRole("option", { name: "125%", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(zoom).toHaveText("125%");
  await expect(zoom).toBeFocused();
  await page.reload();
  await expect(zoom).toHaveText("125%");

  // At 125% the minimum-size native window has about 819 by 480 CSS pixels.
  await page.setViewportSize({ width: 819, height: 480 });
  const bounds = await zoom.boundingBox();
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(819);
  await zoom.click();
  await expect(list).toBeInViewport({ ratio: 1 });
  await page.keyboard.press("Escape");
  await expect(list).toHaveCount(0);
  await expect(zoom).toBeFocused();

  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await expect(zoom).toHaveCount(0);
  await page.keyboard.press("Control+0");
  await page.getByRole("link", { name: "General", exact: true }).click();
  await expect(zoom).toHaveText("100%");
  await page.reload();
  await expect(zoom).toHaveText("100%");
});

test("retains the previous zoom when saving fails, and allows retry", async ({ page }) => {
  await setup(page);
  const zoom = page.getByRole("combobox", { name: "Interface zoom" });
  const reduced = page.getByRole("option", { name: "80%", exact: true });
  await page.evaluate(() => sessionStorage.setItem("mock.zoom.fails", "1"));
  await zoom.click();
  await reduced.click();
  await expect(page.getByRole("alert")).toContainText("Could not change interface zoom");
  await expect(zoom).toHaveText("100%");
  await expect(zoom).toBeFocused();
  await page.evaluate(() => sessionStorage.removeItem("mock.zoom.fails"));
  await zoom.click();
  await reduced.click();
  await expect(zoom).toHaveText("80%");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("shows percentage choices in Chinese", async ({ page }) => {
  await setup(page, true, "zh-CN");
  const zoom = page.getByRole("combobox", { name: "界面缩放" });
  await expect(zoom).toHaveText("100%");
  await zoom.click();
  await expect(page.getByRole("option", { name: "90%", exact: true })).toBeVisible();
});

test("applies local zoom and resets immediately while offline", async ({ page, context }) => {
  await setup(page);
  const zoom = page.getByRole("combobox", { name: "Interface zoom" });
  await expect(zoom).toHaveText("100%");
  await context.setOffline(true);
  await zoom.click();
  await page.getByRole("option", { name: "125%", exact: true }).click();
  await expect(zoom).toHaveText("125%");
  await page.keyboard.press("Control+0");
  await expect(zoom).toHaveText("100%");
  expect(await page.evaluate(() => sessionStorage.getItem("mock.zoom.saved"))).toBe("100");
});

test("a failed read offers an offline retry without claiming a default zoom", async ({ page, context }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem("mock.zoom.readFails", "1");
    sessionStorage.setItem("mock.zoom.saved", "110");
  });
  await setup(page);
  const zoom = page.getByRole("combobox", { name: "Interface zoom" });
  await expect(page.getByRole("alert")).toContainText("Could not read interface zoom");
  await expect(zoom).toBeDisabled();
  await expect(zoom).not.toContainText("100%");
  await context.setOffline(true);
  await page.evaluate(() => sessionStorage.removeItem("mock.zoom.readFails"));
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(zoom).toHaveText("110%");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("a failed shortcut reset is reported outside settings and can be retried", async ({ page }) => {
  await setup(page);
  const zoom = page.getByRole("combobox", { name: "Interface zoom" });
  await zoom.click();
  await page.getByRole("option", { name: "125%", exact: true }).click();
  await expect(zoom).toHaveText("125%");
  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await page.evaluate(() => sessionStorage.setItem("mock.zoom.fails", "1"));
  await page.keyboard.press("Control+0");
  await expect(page.getByText("Could not change interface zoom. Try again.", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("mock.zoom.saved"))).toBe("125");
  await page.evaluate(() => sessionStorage.removeItem("mock.zoom.fails"));
  await page.keyboard.press("Control+0");
  await page.getByRole("link", { name: "General", exact: true }).click();
  await expect(zoom).toHaveText("100%");
});

test("does not show zoom controls when native zoom is unavailable", async ({ page }) => {
  await setup(page, false);
  await expect(page.getByLabel("Interface language", { exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Interface zoom" })).toHaveCount(0);
});
