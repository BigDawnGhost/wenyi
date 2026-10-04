import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

  test("keyboard and pointer use the same rounded dialog controls", async ({
    page,
  }, info) => {
    const desktop = info.project.name === "desktop";
    const origin = desktop ? "http://127.0.0.1:19485" : undefined;
    if (origin) {
      await page.addInitScript((apiBase) => {
        Object.assign(window, { __WENYI_DESKTOP__: { apiBase, token: "focus-fixture-token" } });
      }, origin);
    }
    await fakeApi(page, {}, origin);
    await page.goto(`/projects/${pid}/proofreading/0`);
    await page.getByTestId("translation-text").click({ button: "right" });
    await page.getByRole("menuitem", { name: "Change history", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Paragraph editor", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "Paragraph editor" })).toBeFocused();
    const close = dialog.getByRole("button", { name: "Close", exact: true }).first();
    await expect(close).not.toBeFocused();
    const bounds = (await close.boundingBox())!;
    const clip = { x: bounds.x - 4, y: bounds.y - 4, width: bounds.width + 8, height: bounds.height + 8 };
    const resting = await page.screenshot({ clip, animations: "disabled" });
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
    expect(await page.screenshot({ clip, animations: "disabled" })).toEqual(resting);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  });
