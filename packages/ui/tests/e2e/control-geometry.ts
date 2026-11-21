import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../sandbox/fixtures";


/** Whether the Desktop host must keep running this suite as well.
 *
 * Asserts radio geometry on export and switch geometry plus draft retention in
 * project settings at 1x and 1.25x zoom, without invoking the host's export service.
 */
export const desktopHostSensitive = false;

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
for (const theme of ["light", "dark"]) {
  for (const zoom of [1, 1.25]) {
    test(`${theme} controls at ${zoom} zoom use application geometry and retain drafts`, async ({ page }) => {
      await fakeApi(page, {}, apiOrigin);
      await page.goto(`/projects/${pid}/export`);
      await page.getByRole("radio").first().waitFor();
      await page.evaluate(({ theme, zoom }) => {
        document.documentElement.classList.toggle("dark", theme === "dark");
        document.documentElement.style.zoom = String(zoom);
      }, { theme, zoom });
      for (const input of await page.getByRole("radio").all()) {
        await expect(input).toHaveCSS("appearance", "none");
        await expect(input).toHaveCSS("width", "16px");
        await expect(input).toHaveCSS("height", "16px");
        await expect(input).toHaveCSS("line-height", "16px");
        const geometry = await input.evaluate(node => {
          const input = node.getBoundingClientRect();
          const label = node.closest("label")!.getBoundingClientRect();
          return { size: input.width, center: input.y + input.height / 2 - label.y - label.height / 2 };
        });
        expect(geometry.size).toBeCloseTo(16 * zoom);
        expect(Math.abs(geometry.center)).toBeLessThan(1);
      }
      await page.getByRole("radio", { name: "Bilingual", exact: true }).check();
      await expect(page.getByRole("switch")).toHaveCount(0);
      await page.getByRole("link", { name: "Project settings", exact: true }).click();
      const preferences = page.getByRole("region", { name: "Export preferences", exact: true });
      await expect(preferences.getByRole("switch")).toHaveCount(2);
      await expect(page.getByRole("checkbox")).toHaveCount(0);
      for (const [name, checked] of [
        ["Include an “About this translation” page", true],
        ["Preserve source formatting in bilingual output", false],
      ] as const) {
        const control = preferences.getByRole("switch", { name, exact: true });
        await expect(control).toHaveCSS("width", "36px");
        await expect(control).toHaveCSS("height", "20px");
        await expect(control).toBeChecked({ checked });
        await control.focus();
        await page.keyboard.press("Space");
        await expect(control).toBeChecked({ checked: !checked });
        await page.keyboard.press("Enter");
        await expect(control).toBeChecked({ checked });
        const label = page.locator(`label[for="${await control.getAttribute("id")}"]`);
        await label.click();
        await expect(control).toBeChecked({ checked: !checked });
        await expect(control).toHaveCSS("outline-style", "none");
      }
    });
  }
}
}
