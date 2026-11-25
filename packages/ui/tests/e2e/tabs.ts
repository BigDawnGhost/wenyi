import { test, expect } from "@playwright/test";

/**
 * Whether the Desktop host must keep running this suite as well.
 *
 * Pure DOM semantics of the shared Tabs component; the harness page is shared.
 */
export const desktopHostSensitive = false;

export function registerTests() {
  test("tabs follow the ARIA tabs pattern with caller-provided labels", async ({ page }) => {
    await page.goto("/tests/tabs-harness.html");
    const list = page.getByRole("tablist", { name: "示例标签组" });
    await expect(list).toBeVisible();
    const tabs = list.getByRole("tab");
    await expect(tabs).toHaveCount(3);
    await expect(tabs.first()).toHaveAttribute("aria-selected", "true");
    await expect(tabs.nth(1)).toHaveAttribute("aria-selected", "false");
    await expect(tabs.nth(2)).toHaveAttribute("aria-selected", "false");
    await expect(tabs.first()).toHaveAttribute("tabindex", "0");
    await expect(tabs.nth(1)).toHaveAttribute("tabindex", "-1");
    const panel = page.getByRole("tabpanel");
    await expect(panel).toHaveCount(1);
    await expect(panel).toContainText("概览面板内容");
    await expect(panel).toHaveAttribute("tabindex", "0");
    const [controls, panelId, labelledBy, tabId] = await Promise.all([
      tabs.first().getAttribute("aria-controls"),
      panel.getAttribute("id"),
      panel.getAttribute("aria-labelledby"),
      tabs.first().getAttribute("id"),
    ]);
    expect(panelId).toBeTruthy();
    expect(controls).toBe(panelId);
    expect(labelledBy).toBe(tabId);
  });

  test("arrow keys move focus and Enter activates the focused tab", async ({ page }) => {
    await page.goto("/tests/tabs-harness.html");
    const tabs = page.getByRole("tablist").getByRole("tab");
    await tabs.first().focus();
    await page.keyboard.press("ArrowRight");
    await expect(tabs.nth(1)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("tabpanel")).toContainText("详情面板内容");
    await page.keyboard.press("ArrowRight");
    await expect(tabs.nth(2)).toBeFocused();
    await page.keyboard.press("End");
    await expect(tabs.nth(2)).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(tabs.nth(1)).toBeFocused();
    await page.keyboard.press("Home");
    await expect(tabs.first()).toBeFocused();
  });

  test("clicking a trigger selects it and swaps the visible panel", async ({ page }) => {
    await page.goto("/tests/tabs-harness.html");
    await page.getByRole("tab", { name: "设置" }).click();
    await expect(page.getByRole("tab", { name: "设置" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "false");
    await expect(page.getByRole("tabpanel")).toContainText("设置面板内容");
  });
}
