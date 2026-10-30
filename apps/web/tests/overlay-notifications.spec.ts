import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

test.beforeEach(async ({ page }, info) => {
  if (info.project.name === "desktop") {
    await page.addInitScript(() => {
      Object.assign(window, {
        __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:19485", token: "overlay-fixture-token" },
      });
    });
  }
});

test("glossary options are selectable and failed saves stay readable over the dialog", async ({ page }, info) => {
  await fakeApi(page, {}, info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined);
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
  await expect(toast.getByRole("button", { name: /close/i })).toBeEnabled();
  // Verify the notification paints above the modal, not merely within viewport.
  expect(await toast.evaluate(element => {
    const rect = element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
  })).toBe(true);
  await toast.getByRole("button", { name: /close/i }).click();
  await expect(toast).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("alert")).toContainText("Fixture save failed");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add term", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Add term", exact: true }).click();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
});

test("slow asynchronous feedback ignores scrolling and unrelated button clicks", async ({ page }, info) => {
  const terms = Array.from({ length: 60 }, (_, index) => ({
    source: `fixture-${index}`, target: `translation-${index}`, type: "term",
    reading: "", note: "", gender: "", aliases: [],
  }));
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: terms },
    info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined);
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
