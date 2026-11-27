import { expect, test } from "@playwright/test";
import { expectQuietLabelContrast, expectTokenColor } from "./color-helper";
import { dragText, textPoint } from "./text-selection";

/** Both hosts supply persisted preferences to the shared notification theme. */
export const desktopHostSensitive = true;

export function registerTests() {
  test("mouse selection hits toast text while blank space and gaps still pass clicks through", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/tests/notifications-harness.html");
    await page.getByRole("button", { name: "Notify info" }).click();
    await page.getByRole("button", { name: "Notify error" }).click();
    const notices = page.locator("[data-sonner-toast]");
    const front = notices.filter({ hasText: "error notification" });
    const back = notices.filter({ hasText: "info notification" });
    await expect(front).toHaveAttribute("data-mounted", "true");
    await page.keyboard.press("Alt+t");
    await expect(back).toHaveAttribute("data-expanded", "true");

    const title = front.getByText("error notification", { exact: true });
    const message = "A readable notification in the selected application theme.";
    const description = front.getByText(message, { exact: true });
    expect.soft(await dragText(title)).toBe("error notification");
    expect.soft(await dragText(description)).toBe(message);
    await expect(notices).toHaveCount(2);
    await expect(page.getByRole("button", { name: "Page clicks: 0" })).toBeVisible();

    const card = (await front.boundingBox())!;
    const titleEnd = await textPoint(title, "end");
    await page.mouse.click(titleEnd.x + 8, titleEnd.y);
    await expect(page.getByRole("button", { name: "Page clicks: 1" })).toBeVisible();
    const descriptionEnd = await textPoint(description, "end");
    await page.mouse.click(descriptionEnd.x + 8, descriptionEnd.y);
    await expect(page.getByRole("button", { name: "Page clicks: 2" })).toBeVisible();
    // Move over the close control to expand the stack before clicking its gap.
    const close = front.getByRole("button", { name: "Close toast" });
    await close.hover();
    await expect(back).toHaveAttribute("data-expanded", "true");
    const upper = (await back.boundingBox())!;
    expect(upper.y + upper.height).toBeLessThan(card.y);
    await page.mouse.click(card.x + card.width / 2, (upper.y + upper.height + card.y) / 2);
    await expect(page.getByRole("button", { name: "Page clicks: 3" })).toBeVisible();
    await close.click();
    await expect(front).toHaveCount(0);
    await expect(back).toBeVisible();
  });

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
