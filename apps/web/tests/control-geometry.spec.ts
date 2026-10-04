import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

for (const theme of ["light", "dark"]) {
  for (const zoom of [1, 1.25]) {
    test(`${theme} controls at ${zoom} zoom use application geometry and retain drafts`, async ({ page }, info) => {
      const desktop = info.project.name === "desktop";
      const origin = "http://127.0.0.1:19481";
      if (desktop) await page.addInitScript(origin => {
        Object.assign(window, { __WENYI_DESKTOP__: { apiBase: origin, token: "fixture" } });
      }, origin);
      await fakeApi(page, {}, desktop ? origin : undefined);
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
      const checkbox = page.locator('input[type="checkbox"]').first();
      await expect(checkbox).toBeVisible();
      for (const input of await page.locator('input[type="radio"], input[type="checkbox"]').all()) {
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
      await checkbox.uncheck();
      await summary.click();
      await expect(checkbox).toBeAttached();
      await summary.focus();
      await page.keyboard.press("Space");
      await expect(checkbox).toBeVisible();
      await expect(checkbox).not.toBeChecked();
      await expect(checkbox).toHaveCSS("outline-style", "none");
    });
  }
}
