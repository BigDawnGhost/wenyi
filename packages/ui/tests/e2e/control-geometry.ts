import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../sandbox/fixtures";


/** Whether the Desktop host must keep running this suite as well.
 *
 * Asserts switch and radio geometry plus draft retention at 1x and 1.25x zoom
 * on the shared export page, without invoking the host's export service.
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
      const summary = page.locator("summary");
      await expect(summary.locator("svg")).toHaveCount(1);
      await expect(summary).toHaveCSS("list-style-type", "none");
      expect(await summary.evaluate(node => getComputedStyle(node, "::marker").content)).toBe('""');
      await summary.focus();
      await page.keyboard.press("Enter");
      const punctuation = page.getByRole("switch", {
        name: "Normalize punctuation on export", exact: true, includeHidden: true,
      });
      await expect(punctuation).toBeVisible();
      await expect(page.getByRole("checkbox")).toHaveCount(0);
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
      await expect(page.getByRole("switch")).toHaveCount(3);
      for (const [name, checked] of [
        ["Normalize punctuation on export", true],
        ["Include an “About this translation” page", true],
        ["Preserve source formatting in bilingual output", false],
      ] as const) {
        const control = page.getByRole("switch", { name, exact: true });
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
      }
      await summary.click();
      await expect(punctuation).toBeAttached();
      await summary.focus();
      await page.keyboard.press("Space");
      await expect(punctuation).toBeVisible();
      await expect(punctuation).not.toBeChecked();
      await expect(punctuation).toHaveCSS("outline-style", "none");
    });
  }
}
}
