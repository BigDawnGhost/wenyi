import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../sandbox/fixtures";


/** Whether the Desktop host must keep running this suite as well.
 *
 * Asserts rounded dialog controls have visible keyboard focus; pure CSS and DOM, with no Desktop-only service.
 */
export const desktopHostSensitive = false;

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
  test("keyboard focus adds a visible rounded indicator to dialog controls", async ({
    page,
  }) => {
    await fakeApi(page, {}, apiOrigin);
    await page.goto(`/projects/${pid}/proofreading/0`);
    await page.getByTestId("translation-text").click({ button: "right" });
    await page.getByRole("menuitem", { name: "Change history", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Paragraph editor", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "Paragraph editor" })).toBeFocused();
    const close = dialog.getByRole("button", { name: "Close", exact: true }).first();
    await expect(close).not.toBeFocused();
    const resting = await close.evaluate(element => getComputedStyle(element).boxShadow);
    await page.keyboard.press("Tab");
    await expect(close).toBeFocused();
    const style = await close.evaluate((element) => {
      const css = getComputedStyle(element);
      return {
        outline: css.outlineStyle,
        radius: parseFloat(css.borderRadius),
        shadow: css.boxShadow,
      };
    });
    expect(style.outline).toBe("none");
    expect(style.radius).toBeGreaterThanOrEqual(6);
    expect(style.shadow).not.toBe("none");
    expect(style.shadow).not.toBe(resting);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  });
}
