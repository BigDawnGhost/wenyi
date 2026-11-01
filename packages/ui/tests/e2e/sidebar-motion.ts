import { expect, test, type Locator } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
const sidebarSelector = 'aside[data-slot="sidebar"]';

// Capture the transition in the browser, before a round trip can consume its
// short duration. Seek the real CSS animation rather than sleeping halfway.
async function midpoint(sidebar: Locator, property: string) {
  return sidebar.evaluate(async (element, property) => {
    for (const animation of element.getAnimations({ subtree: true })) animation.play();
    (element.querySelector("button[aria-controls='sidebar-navigation']") as HTMLElement).click();
    for (let frame = 0; frame < 10; frame++) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      const animations = element.getAnimations({ subtree: true });
      const transition = animations.find(
        (animation) => animation instanceof CSSTransition && animation.transitionProperty === property,
      );
      if (!transition) continue;
      const duration = Number(transition.effect!.getTiming().duration);
      for (const animation of animations) {
        animation.pause();
        animation.currentTime = Number(animation.effect!.getTiming().duration) / 2;
      }
      const rect = element.getBoundingClientRect();
      return { width: rect.width, height: rect.height, duration };
    }
    throw new Error(`No sidebar ${property} transition started`);
  }, property);
}

async function finish(sidebar: Locator) {
  await sidebar.evaluate((element) => {
    for (const animation of element.getAnimations({ subtree: true })) animation.finish();
  });
}

async function noOverflow(sidebar: Locator) {
  expect(await sidebar.evaluate(() =>
    document.documentElement.scrollWidth <= document.documentElement.clientWidth,
  )).toBe(true);
}

test.describe("sidebar motion", () => {
  test.beforeEach(async ({ page }, info) => {
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.addInitScript(() => localStorage.setItem("wenyi.locale", "en"));
    await fakeApi(page, {}, apiOrigin);
  });

  test("desktop width interpolates both ways and icon links remain usable", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 960 });
    await page.goto(`/projects/${pid}/proofreading`);
    const sidebar = page.locator(sidebarSelector);
    await expect(sidebar).toHaveCSS("width", "240px");
    const collapse = await midpoint(sidebar, "width");
    expect(collapse.duration).toBeGreaterThanOrEqual(250);
    expect(collapse.duration).toBeLessThanOrEqual(320);
    expect(collapse.width).toBeGreaterThan(64);
    expect(collapse.width).toBeLessThan(240);
    await noOverflow(sidebar);
    await finish(sidebar);
    await expect(sidebar).toHaveCSS("width", "64px");
    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    const settings = sidebar.getByRole("link", { name: "Project settings", exact: true });
    await expect(settings).toHaveAttribute("title", "Project settings");
    await settings.click();
    await expect(page).toHaveURL(`/projects/${pid}/settings`);
    const expansion = await midpoint(sidebar, "width");
    expect(expansion.width).toBeGreaterThan(64);
    expect(expansion.width).toBeLessThan(240);
    await finish(sidebar);
    await expect(sidebar).toHaveCSS("width", "240px");
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");
    await noOverflow(sidebar);
  });

  test("reversing an unfinished collapse settles expanded and survives reload", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 960 });
    await page.goto(`/projects/${pid}/proofreading`);
    const sidebar = page.locator(sidebarSelector);
    await expect(sidebar).toHaveCSS("width", "240px");
    await midpoint(sidebar, "width");
    // Reverse while the collapse is still in flight, not after its end state.
    await sidebar.evaluate((element) => {
      for (const animation of element.getAnimations({ subtree: true })) animation.play();
      (element.querySelector("button[aria-controls='sidebar-navigation']") as HTMLElement).click();
    });
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");
    await expect(sidebar).toHaveCSS("width", "240px");
    await page.reload();
    await expect(sidebar).toHaveCSS("width", "240px");
    await expect(sidebar.getByRole("button", { name: "Collapse sidebar", exact: true }))
      .toHaveAttribute("aria-expanded", "true");
    await noOverflow(sidebar);
  });

  test("narrow desktop navigation animates width without hiding its links", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/projects/${pid}/proofreading`);
    const sidebar = page.locator(sidebarSelector);
    const navigation = sidebar.getByRole("navigation", { name: "Project navigation" });
    await expect(navigation).toBeVisible();
    const middle = await midpoint(sidebar, "width");
    await noOverflow(sidebar);
    await finish(sidebar);
    await expect(navigation).toBeVisible();
    await expect(sidebar).toHaveCSS("width", "64px");
    expect(middle.width).toBeGreaterThan(64);
    expect(middle.width).toBeLessThan(240);
    await expect(sidebar.getByRole("button", { name: "Expand sidebar", exact: true })).toBeInViewport();
    await midpoint(sidebar, "width");
    await finish(sidebar);
    await expect(navigation).toBeVisible();
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");
    await noOverflow(sidebar);
  });

  test("sidebar motion ignores system motion preference and reload does not replay it", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 960 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`/projects/${pid}/proofreading`);
    const sidebar = page.locator(sidebarSelector);
    await expect(sidebar).toHaveCSS("width", "240px");
    const collapse = await midpoint(sidebar, "width");
    expect(collapse.width).toBeGreaterThan(64);
    expect(collapse.width).toBeLessThan(240);
    await finish(sidebar);
    await expect(sidebar).toHaveCSS("width", "64px");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    // Observe transitionrun from document creation so a transient mount
    // animation cannot finish unnoticed before the locator becomes available.
    await page.addInitScript(() => {
      document.addEventListener("transitionrun", (event) => {
        if ((event.target as Element).closest('aside[data-slot="sidebar"]')) {
          document.documentElement.dataset.sidebarTransitionRan = "true";
        }
      });
    });
    await page.reload();
    await expect(sidebar).toHaveCSS("width", "64px");
    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    await expect(page.locator("html")).not.toHaveAttribute("data-sidebar-transition-ran", "true");
    await noOverflow(sidebar);
  });
});
}
