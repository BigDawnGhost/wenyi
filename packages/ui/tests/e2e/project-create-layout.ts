import { expect, test, type Locator } from "@playwright/test";
import { capabilities, fakeApi } from "../../sandbox/fixtures";

/** The native folder picker changes the form geometry, so both hosts run this suite. */
export const desktopHostSensitive = true;

async function bounds(locator: Locator) {
  await expect(locator).toBeVisible();
  return (await locator.boundingBox())!;
}

export function registerTests({ projectLocation = false }: { projectLocation?: boolean } = {}) {
  for (const chinese of [false, true]) {
    for (const dark of [false, true]) {
      test(`compact creation aligns fields in ${chinese ? "Chinese" : "English"} in ${dark ? "dark" : "light"}`, async ({ page }) => {
        await page.setViewportSize({ width: 1024, height: 640 });
        await page.addInitScript(
          locale => localStorage.setItem("wenyi.locale", locale),
          chinese ? "zh-CN" : "en",
        );
        await fakeApi(page, {
          "/capabilities": {
            ...capabilities,
            input_formats: ["epub", "fb2", "txt", "markdown", "html", "pdf", "docx"],
          },
        });
        await page.goto("/projects/new");
        await page.evaluate(dark => document.documentElement.classList.toggle("dark", dark), dark);
        const dialog = page.getByRole("dialog", { name: chinese ? "创建项目" : "Create project", exact: true });
        const source = await bounds(page.getByLabel(chinese ? "源语言" : "Source language", { exact: true }));
        const target = await bounds(page.getByLabel(chinese ? "目标语言" : "Target language", { exact: true }));
        const style = page.getByLabel(chinese ? "翻译风格" : "Translation style", { exact: true });
        const genre = await bounds(style);
        expect(source.y).toBeCloseTo(target.y, 0);
        expect(source.y).toBeCloseTo(genre.y, 0);
        expect(source.x).toBeLessThan(target.x);
        expect(target.x).toBeLessThan(genre.x);
        await expect(style).toHaveText(chinese ? "文学" : "Literary");
        const zone = await bounds(page.getByRole("group", {
          name: chinese ? "原文文件选择区域" : "Source file selection",
        }));
        expect(zone.y).toBeGreaterThan(genre.y + genre.height);
        const folder = page.getByLabel(chinese ? "项目文件夹" : "Project folder", { exact: true });
        if (projectLocation) {
          const location = await bounds(folder);
          const label = await bounds(dialog.locator('label[for="project-location"]'));
          expect(location.y).toBeGreaterThan(zone.y + zone.height);
          expect(label.y + label.height / 2).toBeCloseTo(location.y + location.height / 2, 0);
        } else {
          await expect(folder).toHaveCount(0);
        }
        const precision = page.getByRole("switch", { name: chinese ? "三译合润" : "Three drafts + synthesis", exact: true });
        const prepare = page.getByRole("switch", { name: chinese ? "创建后执行译前准备" : "Prepare before translating", exact: true });
        const first = await bounds(precision);
        const second = await bounds(prepare);
        expect(first.y).toBeCloseTo(second.y, 0);
        await expect(precision).not.toBeChecked();
        await expect(prepare).not.toBeChecked();
        await prepare.focus();
        await page.keyboard.press("Space");
        await expect(prepare).toBeChecked();
        await page.keyboard.press("Enter");
        await expect(prepare).not.toBeChecked();
        expect((await bounds(dialog)).height).toBeLessThanOrEqual(600);
        expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
        await expect(dialog.getByRole("button", {
          name: chinese ? "创建项目" : "Create project", exact: true,
        })).toBeInViewport();
      });
    }
  }
}
