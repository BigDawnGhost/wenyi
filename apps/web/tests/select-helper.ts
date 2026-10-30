import { expect, type Locator } from "@playwright/test";

/** Operate the visible popup, never Radix's internal form compatibility control. */
export async function chooseOption(trigger: Locator, name: string | RegExp) {
  await expect(trigger).toHaveAttribute("role", "combobox");
  await trigger.click();
  const list = trigger.page().getByRole("listbox");
  await expect(list).toBeVisible();
  const option = list.getByRole("option", { name, exact: true });
  await expect(option).toBeVisible();
  await option.click();
  await expect(list).toBeHidden();
  await expect(trigger).toHaveText(name);
}
