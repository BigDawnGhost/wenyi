import { expect, test } from "@playwright/test";
import { expectQuietLabelContrast, expectTokenColor } from "./color-helper";

/** Both hosts supply persisted preferences to the shared notification theme. */
export const desktopHostSensitive = true;

export function registerTests() {
  for (const theme of ["light", "dark"] as const) {
    test(`every notification type follows the explicit ${theme} theme`, async ({ page }, info) => {
      await page.emulateMedia({ colorScheme: theme === "dark" ? "light" : "dark" });
      await page.addInitScript(theme => localStorage.setItem("wenyi.theme", theme), theme);
      await page.goto("/tests/notifications-harness.html");
      for (const kind of ["success", "error", "warning", "info", "message"]) {
        await page.getByRole("button", { name: `Notify ${kind}`, exact: true }).click();
        const notice = page.locator("[data-sonner-toast]").filter({ hasText: `${kind} notification` });
        await expect(notice).toBeVisible();
        await expect(page.locator("[data-sonner-toaster]")).toHaveAttribute("data-theme", theme);
        const tone = kind === "message" ? "card" : `status-${kind === "error" ? "danger" : kind}`;
        await expectTokenColor(notice, "background-color", tone);
        await expectTokenColor(notice, "color", `${tone}-foreground`);
        await expectTokenColor(notice, "border-top-color",
          kind === "message" ? "border" : `${tone}-border`);
        await expectQuietLabelContrast(notice);
        const close = notice.getByRole("button", { name: "Close toast", exact: true });
        await close.hover();
        await expectTokenColor(close, "background-color", "hover");
        if (kind !== "message") {
          await expectTokenColor(notice.locator("[data-icon]"), "color", `${tone}-foreground`);
        }
        await notice.screenshot({ path: info.outputPath(`notification-${theme}-${kind}.png`) });
        await close.click();
        await expect(notice).toHaveCount(0);
      }
    });
  }

  test("an open notification follows application and system theme switches", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.addInitScript(() => localStorage.setItem("wenyi.theme", "light"));
    await page.goto("/tests/notifications-harness.html");
    await page.getByRole("button", { name: "Notify error", exact: true }).click();
    const notice = page.locator("[data-sonner-toast]");
    const toaster = page.locator("[data-sonner-toaster]");
    await page.getByRole("button", { name: "Use dark theme", exact: true }).click();
    await expect(toaster).toHaveAttribute("data-theme", "dark");
    await expectTokenColor(notice, "background-color", "status-danger");
    const darkColor = await notice.evaluate(element => getComputedStyle(element).backgroundColor);
    await page.getByRole("button", { name: "Use system theme", exact: true }).click();
    await expect(toaster).toHaveAttribute("data-theme", "light");
    await expectTokenColor(notice, "background-color", "status-danger");
    await expect(notice).not.toHaveCSS("background-color", darkColor);
    await page.emulateMedia({ colorScheme: "dark" });
    await expect(toaster).toHaveAttribute("data-theme", "dark");
    await expect(notice).toHaveCSS("background-color", darkColor);
    await expectQuietLabelContrast(notice);
  });
}
