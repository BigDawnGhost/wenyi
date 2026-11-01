import { expect, test } from "@playwright/test";
import { createTranslator } from "../../src/i18n/catalog";
import { fakeApi, pid } from "./fixtures";

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
test.beforeEach(async ({ page }) => {
  await fakeApi(page, {}, apiOrigin);
});

test.describe("phone entry points", () => {
  test.use({
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
  });
  for (const locale of ["en", "zh-CN"] as const) {
    test(`mobile entry points show only the computer notice in ${locale}`, async ({ page }) => {
      await page.addInitScript((locale) => localStorage.setItem("wenyi.locale", locale), locale);
      await page.setViewportSize({ width: 390, height: 844 });
      const requests: string[] = [];
      page.on("request", (request) => {
        if (["fetch", "xhr"].includes(request.resourceType())) requests.push(request.url());
      });
      page.on("websocket", (socket) => {
        if (new URL(socket.url()).pathname.startsWith("/ws/")) requests.push(socket.url());
      });
      for (const path of ["/", "/settings", `/projects/${pid}`, "/projects/new"]) {
        await page.goto(path);
        const title = locale === "en" ? "Please use a computer" : "请使用电脑打开";
        const notice = page.getByRole("heading", { name: title, exact: true });
        await expect(notice).toBeVisible();
        await expect(page.locator('[data-slot="sidebar"]')).toHaveCount(0);
        await page.keyboard.press("Escape");
        await expect(notice).toBeVisible();
        expect(await page.evaluate(() =>
          document.documentElement.scrollWidth <= innerWidth,
        )).toBe(true);
      }
      expect(requests).toEqual([]);
    });
  }
});

test("desktop windows remain usable at any width without resetting a settings draft", async ({ page }) => {
  await page.goto("/settings/providers");
  const field = page.getByLabel("API base URL", { exact: true });
  await field.fill("https://unsaved.example/v1");
  await field.focus();
  for (const width of [1024, 800, 390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole("heading", { name: "Please use a computer" })).toHaveCount(0);
    await expect(page.locator('[data-slot="sidebar"]')).toHaveCSS("width", "240px");
    await expect(field).toHaveValue("https://unsaved.example/v1");
    await expect(field).toBeFocused();
  }
});

test("a narrow computer keeps side-by-side reading with horizontal content scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/projects/${pid}/proofreading/0`);
  const main = page.getByRole("main");
  const paragraph = page.locator("#paragraph-0");
  await expect(paragraph).toBeVisible();
  const source = await paragraph.locator(":scope > div").first().boundingBox();
  const target = await paragraph.locator(":scope > div").nth(1).boundingBox();
  expect(target!.y).toBe(source!.y);
  expect(target!.x).toBeGreaterThan(source!.x);
  expect(await main.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  await main.evaluate(element => { element.scrollLeft = element.scrollWidth; });
  await paragraph.getByRole("button", { name: "Paragraph actions" }).click();
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Paragraph editor" })).toBeVisible();
});

test("resizing a computer window does not dismiss an open dialog or discard its draft", async ({ page }) => {
  const t = createTranslator("en");
  await page.goto("/projects/new");
  const creation = page.getByRole("dialog", { name: t("common.createProject"), exact: true });
  const name = creation.getByLabel(t("createProject.projectName"), { exact: true });
  await name.fill("Unsaved project");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { name: "Please use a computer" })).toHaveCount(0);
  await expect(creation).toBeVisible();
  await expect(page).toHaveURL(/\/projects\/new$/);
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(creation).toBeVisible();
  await expect(name).toHaveValue("Unsaved project");
  await expect(name).toBeFocused();
});

for (const [device, userAgent] of [
  ["iPhone", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"],
  ["Android", "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130.0.0.0 Mobile Safari/537.36"],
  ["iPad", "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari/604.1"],
] as const) {
  test.describe(device, () => {
    test.use({ userAgent, viewport: { width: 1440, height: 900 } });
    test("a wide mobile viewport cannot bypass the computer requirement", async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "Please use a computer" })).toBeVisible();
      await expect(page.locator('[data-slot="sidebar"]')).toHaveCount(0);
    });
  });
}

test.describe("touchscreen desktop", () => {
  test.use({
    hasTouch: true,
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
  });
  test("touch support alone does not block a computer", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "My projects" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Please use a computer" })).toHaveCount(0);
  });
});

test("platform and touch hints do not override a desktop user agent", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "platform", { get: () => "MacIntel" });
    Object.defineProperty(navigator, "maxTouchPoints", { get: () => 5 });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "My projects" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Please use a computer" })).toHaveCount(0);
});
}
