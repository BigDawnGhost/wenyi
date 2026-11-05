import { expect, test, type Locator } from "@playwright/test";
import { fakeApi, pid } from "../../sandbox/fixtures";

async function expectInsetCloseButton(toast: Locator) {
  const close = toast.getByRole("button", { name: /close/i });
  await expect(close).toBeEnabled();
  const card = (await toast.boundingBox())!;
  const button = (await close.boundingBox())!;
  const content = (await toast.locator("[data-content]").boundingBox())!;
  expect(button.width).toBeGreaterThanOrEqual(44);
  expect(button.height).toBeGreaterThanOrEqual(44);
  expect(button.x).toBeGreaterThan(card.x + card.width / 2);
  expect(button.y).toBeGreaterThan(card.y);
  expect(button.x + button.width).toBeLessThan(card.x + card.width);
  expect(button.y + button.height).toBeLessThanOrEqual(card.y + card.height);
  expect(content.x + content.width).toBeLessThanOrEqual(button.x - 8);
  return close;
}

/** Whether the Desktop host must keep running this suite as well.
 *
 * Asserts toasts stay readable above an open dialog and that slow feedback ignores scroll and stray clicks; z-order and pointer handling, not transport. The probe recorded no Desktop-only service.
 */
export const desktopHostSensitive = false;

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {

test("glossary options are selectable and failed saves stay readable over the dialog", async ({ page }, info) => {
  await fakeApi(page, {}, apiOrigin);
  await page.route(`**/projects/${pid}/glossary/terms`, async route => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 500, json: { detail: "Fixture save failed" } });
    } else {
      await route.fallback();
    }
  });
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Add term", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Add term", exact: true });
  const type = dialog.getByRole("combobox", { name: "Type", exact: true });
  await type.click();
  const option = page.getByRole("option").last();
  const label = await option.innerText();
  await option.click();
  await expect(type).toContainText(label);
  await dialog.locator("input").nth(0).fill("fixture");
  await dialog.locator("input").nth(1).fill("translation");
  await dialog.getByRole("button", { name: "Add", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("Fixture save failed");
  const toast = page.locator("[data-sonner-toast]").filter({ hasText: "Fixture save failed" });
  await expect(toast).toBeInViewport();
  await page.waitForTimeout(1600);
  await expect(toast).toBeVisible();
  const close = await expectInsetCloseButton(toast);
  // Verify the notification paints above the modal, not merely within viewport.
  expect(await toast.evaluate(element => {
    const rect = element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
  })).toBe(true);
  // The whole target is clickable, not just the cross at its center.
  await close.click({ position: { x: 4, y: 4 } });
  await expect(toast).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("alert")).toContainText("Fixture save failed");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add term", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Add term", exact: true }).click();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
});

for (const width of [1280, 420]) {
  test(`long notifications keep an inset, keyboard-accessible close button at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 800 });
    await fakeApi(page, {
      [`/projects/${pid}/glossary/terms`]: [{
        source: "fixture", target: "translation", type: "term",
        reading: "", note: "", gender: "", aliases: [],
      }],
    }, apiOrigin);
    const message = "术语保存失败，请稍后重试。The terminology service is temporarily unavailable: "
      + "a-long-unbroken-request-identifier-".repeat(4);
    await page.route(`**/projects/${pid}/glossary/terms/fixture`, route =>
      route.fulfill({ status: 500, json: { detail: message } }),
    );
    await page.goto(`/projects/${pid}/glossary`);
    await page.getByRole("button", { name: "Delete term", exact: true }).first().click();
    const toast = page.locator("[data-sonner-toast]").filter({ hasText: message });
    await expect(toast).toBeInViewport();
    await expect(toast).toHaveAttribute("data-mounted", "true");
    await page.keyboard.press("Alt+t");
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    const close = toast.getByRole("button", { name: /close/i });
    await expect(close).toBeFocused();
    await info.attach("notification-layout", {
      body: await page.screenshot({ animations: "disabled" }),
      contentType: "image/png",
    });
    await expectInsetCloseButton(toast);
    expect(await toast.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.keyboard.press("Enter");
    await expect(toast).toHaveCount(0);
  });
}

test("slow asynchronous feedback ignores scrolling and unrelated button clicks", async ({ page }, info) => {
  const terms = Array.from({ length: 60 }, (_, index) => ({
    source: `fixture-${index}`, target: `translation-${index}`, type: "term",
    reading: "", note: "", gender: "", aliases: [],
  }));
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: terms },
    apiOrigin);
  let release = () => {};
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/projects/${pid}/glossary/terms/fixture-0`, async route => {
    await gate;
    await route.fulfill({ status: 500, json: { detail: "Slow fixture failed" } });
  });
  await page.goto(`/projects/${pid}/glossary`);
  const requested = page.waitForRequest(`**/projects/${pid}/glossary/terms/fixture-0`);
  await page.getByRole("button", { name: "Delete term", exact: true }).first().click();
  await requested;
  // Real unrelated controls must not steal the pending request's feedback.
  await page.getByRole("button", { name: "Export", exact: true }).click();
  const scroll = await page.evaluate(() => {
    const content = document.querySelector<HTMLElement>('[data-slot="content"]')!;
    content.scrollTop = content.scrollHeight;
    return content.scrollTop;
  });
  expect(scroll).toBeGreaterThan(0);
  await page.waitForTimeout(5500);
  await page.getByRole("button", { name: "Edit term", exact: true }).last().click();
  await page.keyboard.press("Escape");
  release();
  const toast = page.locator("[data-sonner-toast]").filter({ hasText: "Slow fixture failed" });
  await expect(toast).toBeInViewport({ timeout: 10000 });
  await expect(toast).toHaveAttribute("data-mounted", "true");
  await expect(page.locator("[data-sonner-toaster]")).toHaveCSS("position", "fixed");
  await page.waitForTimeout(400);
  const before = await toast.boundingBox();
  await page.evaluate(() => {
    document.querySelector<HTMLElement>('[data-slot="content"]')!.scrollTop = 0;
  });
  await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
  await expect(toast).toBeInViewport();
  const after = await toast.boundingBox();
  expect(after!.x).toBeCloseTo(before!.x, 0);
  expect(after!.y).toBeCloseTo(before!.y, 0);
});
}
