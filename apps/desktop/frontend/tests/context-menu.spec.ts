import { expect, test, type Locator, type Page } from "@playwright/test";
import { fakeApi, pid } from "../../../../packages/ui/sandbox/fixtures";

async function desktop(page: Page) {
  await page.addInitScript(() => {
    window.__WENYI_DESKTOP__ = {
      apiBase: `${location.origin}/api`, token: "context-menu-fixture-token",
    };
  });
  await fakeApi(page);
}

async function rightClickIsPrevented(target: Locator) {
  await target.evaluate(() => {
    // Observe after React and the document handler have both received the event.
    window.addEventListener("contextmenu", (event) => {
      document.documentElement.dataset.contextMenuPrevented = String(event.defaultPrevented);
    }, { once: true });
  });
  await target.click({ button: "right" });
  return await target.page().locator("html").getAttribute("data-context-menu-prevented") === "true";
}

for (const state of ["pending", "error", "closing"] as const) {
  test(`Desktop suppresses the browser menu while ${state}`, async ({ page }) => {
    await page.addInitScript((state) => {
      delete window.__WENYI_DESKTOP__;
      if (state === "error") window.__WENYI_DESKTOP_ERROR__ = "Fixture startup failure";
      if (state === "closing") window.__WENYI_DESKTOP_CLOSING__ = true;
    }, state);
    await page.goto("/");
    expect(await rightClickIsPrevented(page.locator("main"))).toBe(true);
  });
}

test("Desktop suppresses the browser menu after startup and navigation", async ({ page }) => {
  await desktop(page);
  await page.goto("/");
  expect(await rightClickIsPrevented(page.getByRole("heading", { name: "My projects" }))).toBe(true);
  expect(await rightClickIsPrevented(page.getByRole("link", { name: "Settings", exact: true }))).toBe(true);
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  expect(await rightClickIsPrevented(page.getByRole("heading", { name: "General", exact: true }))).toBe(true);
  await page.reload();
  expect(await rightClickIsPrevented(page.getByRole("heading", { name: "General", exact: true }))).toBe(true);
});

test("Desktop preserves native text menus but not menus on non-text controls", async ({ page }) => {
  await page.addInitScript(() => { delete window.__WENYI_DESKTOP__; });
  await page.goto("/");
  await expect(page.locator('[data-runtime-state="pending"]')).toBeVisible();
  await page.evaluate(() => {
    const fields = document.createElement("section");
    fields.innerHTML = `
      <input aria-label="Text" value="Editable text">
      <input aria-label="Password" type="password" value="fixture">
      <input aria-label="Read only" readonly value="Copyable text">
      <textarea aria-label="Multiline">Editable text</textarea>
      <div contenteditable="true"><span data-testid="editable">Editable text</span>
        <span contenteditable="false" data-testid="noneditable">Not editable</span>
      </div>
      <input aria-label="Checkbox" type="checkbox">
      <button>Fixture button</button>
    `;
    document.body.append(fields);
  });
  for (const label of ["Text", "Password", "Read only", "Multiline"]) {
    expect(await rightClickIsPrevented(page.getByLabel(label, { exact: true }))).toBe(false);
    await page.keyboard.press("Escape");
  }
  expect(await rightClickIsPrevented(page.getByTestId("editable"))).toBe(false);
  await page.keyboard.press("Escape");
  for (const target of [
    page.getByTestId("noneditable"),
    page.getByLabel("Checkbox"),
    page.getByRole("button", { name: "Fixture button" }),
  ]) {
    expect(await rightClickIsPrevented(target)).toBe(true);
  }
});

test("Desktop keeps the paragraph menu and the editor's native text menu", async ({ page }) => {
  await desktop(page);
  await page.goto(`/projects/${pid}/proofreading/0`);
  expect(await rightClickIsPrevented(page.getByTestId("translation-text"))).toBe(true);
  await expect(page.getByRole("menu", { name: "Paragraph actions", exact: true })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Copy source", exact: true })).toBeVisible();
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  const editor = page.getByLabel("Edit translation", { exact: true });
  expect(await rightClickIsPrevented(editor)).toBe(false);
  await editor.click();
  await editor.fill("Edited with the native context menu still available");
  await expect(editor).toHaveValue("Edited with the native context menu still available");
});
