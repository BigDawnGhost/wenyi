import { expect, test, type Page } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/sandbox/fixtures";

/** Resolve the element a control's aria-describedby points at. */
function describedTarget(page: Page, selector: string) {
  return page.evaluate((sel) => {
    const control = document.querySelector(sel);
    const id = control?.getAttribute("aria-describedby") ?? null;
    const target = id ? document.getElementById(id) : null;
    return {
      describedBy: id,
      role: target?.getAttribute("role") ?? null,
      text: target?.textContent ?? null,
    };
  }, selector);
}

// Task 9.8: form validation errors must be exposed to assistive technology —
// the failing control carries aria-invalid and references the alert text
// through aria-describedby.
test("Provider form wires validation errors into aria-invalid/aria-describedby", async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
    });
  });
  await fakeApi(page);
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Add provider" }).click();

  // The openai-compatible kind makes the base URL natively required; fill it so
  // the form reaches the component's own validation.
  await page.getByLabel("API base URL").fill("https://api.example.com/v1");

  // A duplicate connection id flags the id input.
  await page.getByLabel("Connection ID").fill("deepseek");
  await page.getByRole("button", { name: "Save provider" }).click();
  const alert = page.getByRole("alert").filter({ hasText: "This ID already exists." });
  await expect(alert).toBeVisible();
  const idInput = page.locator("#provider-id");
  await expect(idInput).toHaveAttribute("aria-invalid", "true");
  const idTarget = await describedTarget(page, "#provider-id");
  expect(idTarget.role).toBe("alert");
  expect(idTarget.text).toContain("This ID already exists.");

  // A blank model name flags the model input instead once the id is unique.
  await page.getByLabel("Connection ID").fill("openai");
  await page.getByRole("button", { name: "Save provider" }).click();
  const modelAlert = page.getByRole("alert").filter({ hasText: "Enter a remote model name" });
  await expect(modelAlert).toBeVisible();
  const modelInput = page.locator("#name-0");
  await expect(modelInput).toHaveAttribute("aria-invalid", "true");
  const modelTarget = await describedTarget(page, "#name-0");
  expect(modelTarget.role).toBe("alert");
  expect(modelTarget.text).toContain("Enter a remote model name");

  // Fixing the input clears the invalid state again.
  await modelInput.fill("gpt-4o");
  await expect(modelInput).not.toHaveAttribute("aria-invalid");
  await expect(modelInput).not.toHaveAttribute("aria-describedby");
});
