import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

for (const chinese of [false, true]) {
  test.describe(`collapsible navigation in ${chinese ? "Chinese" : "English"}`, () => {
    const collapseLabel = chinese ? "收起侧边栏" : "Collapse sidebar";
    const expandLabel = chinese ? "展开侧边栏" : "Expand sidebar";
    const projectLabel = chinese ? "项目导航" : "Project navigation";
    const globalLabel = chinese ? "全局导航" : "Global navigation";
    const proofreadingLabel = chinese ? "人工校阅" : "Manual proofreading";

    test.beforeEach(async ({ page }) => {
      await page.addInitScript(
        (locale) => localStorage.setItem("wenyi.locale", locale),
        chinese ? "zh-CN" : "en",
      );
      await fakeApi(page);
    });

    test("desktop icon navigation keeps links accessible and remembers its width", async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width: 1440, height: 960 });
      await page.goto(`/projects/${pid}/proofreading`);
      const sidebar = page.getByRole("complementary");
      const main = page.getByRole("main");
      const navigation = page.getByRole("navigation", { name: projectLabel });
      const expandedWidth = await main.evaluate(
        (element) => element.clientWidth,
      );
      const toggle = page.getByRole("button", {
        name: collapseLabel,
        exact: true,
      });
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
      await toggle.focus();
      await page.keyboard.press("Enter");
      const expand = page.getByRole("button", {
        name: expandLabel,
        exact: true,
      });
      await expect(expand).toHaveAttribute("aria-expanded", "false");
      await expect(sidebar).toHaveCSS("width", "64px");
      expect(
        await main.evaluate((element) => element.clientWidth),
      ).toBeGreaterThan(expandedWidth + 100);
      await expect(navigation.getByRole("link")).toHaveCount(9);
      const current = navigation.getByRole("link", {
        name: proofreadingLabel,
        exact: true,
      });
      await expect(current).toBeVisible();
      await expect(current).toHaveAttribute("aria-current", "page");
      await expect(current).toHaveAttribute("title", proofreadingLabel);
      await expect(sidebar.getByRole("link")).toHaveCount(12);
      for (const link of await sidebar.getByRole("link").all()) {
        await expect(link).toBeInViewport();
      }
      await page.screenshot({
        path: testInfo.outputPath("collapsed-sidebar.png"),
      });
      await navigation
        .getByRole("link", {
          name: chinese ? "项目配置" : "Project settings",
          exact: true,
        })
        .click();
      await expect(page).toHaveURL(`/projects/${pid}/settings`);
      await expect(expand).toBeVisible();
      await page
        .getByRole("navigation", { name: globalLabel })
        .getByRole("link", { name: chinese ? "设置" : "Settings", exact: true })
        .click();
      await expect(page).toHaveURL("/settings");
      await page.reload();
      await expect(expand).toBeVisible();
      await expect(sidebar).toHaveCSS("width", "64px");
      await expand.click();
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
      await expect(sidebar).toHaveCSS("width", "240px");
      await page.reload();
      await expect(toggle).toBeVisible();
      await expect(sidebar).toHaveCSS("width", "240px");
    });

    test("mobile navigation collapses without hiding its restore control", async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto(`/projects/${pid}/proofreading`);
      const navigation = page.getByRole("navigation", { name: projectLabel });
      const global = page.getByRole("navigation", { name: globalLabel });
      await expect(navigation).toBeVisible();
      const main = page.getByRole("main");
      const expandedHeight = await main.evaluate(
        (element) => element.clientHeight,
      );
      await page
        .getByRole("button", { name: collapseLabel, exact: true })
        .click();
      const expand = page.getByRole("button", {
        name: expandLabel,
        exact: true,
      });
      await expect(expand).toBeInViewport();
      await expect(navigation).not.toBeVisible();
      await expect(global).not.toBeVisible();
      expect(
        await main.evaluate((element) => element.clientHeight),
      ).toBeGreaterThan(expandedHeight + 100);
      await page.reload();
      await expect(expand).toBeVisible();
      await expect(navigation).not.toBeVisible();
      await page.screenshot({
        path: testInfo.outputPath("collapsed-mobile-navigation.png"),
      });
      await page.setViewportSize({ width: 1280, height: 960 });
      await expect(navigation).toBeVisible();
      await expect(global).toBeVisible();
      await expect(page.getByRole("complementary")).toHaveCSS("width", "64px");
      await page.setViewportSize({ width: 390, height: 844 });
      await expect(navigation).not.toBeVisible();
      await expand.focus();
      await page.keyboard.press("Space");
      await expect(navigation).toBeVisible();
      await expect(global).toBeVisible();
      await navigation
        .getByRole("link", { name: proofreadingLabel, exact: true })
        .click();
      await expect(page).toHaveURL(`/projects/${pid}/proofreading`);
      expect(
        await main.evaluate(
          (element) => element.scrollWidth <= element.clientWidth,
        ),
      ).toBe(true);
    });
  });
}

test("sidebar toggles remain usable when browser preference storage is unavailable", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const getItem = Storage.prototype.getItem;
    const setItem = Storage.prototype.setItem;
    Storage.prototype.getItem = function (key) {
      if (key === "wenyi.sidebarCollapsed")
        throw new DOMException("Storage unavailable", "SecurityError");
      return getItem.call(this, key);
    };
    Storage.prototype.setItem = function (key, value) {
      if (key === "wenyi.sidebarCollapsed")
        throw new DOMException("Storage unavailable", "SecurityError");
      setItem.call(this, key, value);
    };
  });
  await fakeApi(page);
  await page.goto("/");
  await page
    .getByRole("button", { name: "Collapse sidebar", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Expand sidebar", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Collapse sidebar", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
});
