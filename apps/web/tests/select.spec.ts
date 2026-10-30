import { test, expect } from "@playwright/test";
import { fakeApi } from "./fixtures";
import { chooseOption } from "./select-helper";

test.beforeEach(async ({ page, baseURL }, info) => {
  if (info.project.name === "desktop") {
    await page.addInitScript((apiBase) => {
      Object.assign(window, {
        __WENYI_DESKTOP__: {
          apiBase,
          token: "fake-select-test-token",
        },
      });
    }, `${baseURL}/api`);
  }
});

test("dark OS cannot take over a light language popup", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await fakeApi(page);
  await page.goto("/settings");
  await page.locator("html").evaluate((element) => {
    element.classList.remove("dark");
    element.classList.add("light");
  });
  await page.getByRole("combobox", { name: "Interface language" }).click();
  const list = page.getByRole("listbox");
  await expect(list).toBeVisible();
  await expect(list).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(list.getByRole("option", { name: "English", exact: true })).toBeVisible();
  await expect(list.getByRole("option", { name: "简体中文", exact: true })).toBeVisible();
});

test("explicit dark theme controls the portalled options", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await fakeApi(page);
  await page.goto("/settings");
  await page.locator("html").evaluate((element) => element.classList.add("dark"));
  await page.getByLabel("Interface language").click();
  await expect(page.getByRole("listbox")).toHaveCSS("background-color", "rgb(9, 9, 11)");
  await expect(page.getByRole("listbox")).toHaveCSS("color", "rgb(250, 250, 250)");
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("Interface language")).toBeFocused();
});

test("keyboard, empty values, disabled options, defaults and actual form values", async ({ page }) => {
  await page.goto("/tests/select-harness.html");
  const trigger = page.getByRole("combobox", { name: "受控语言" });
  await expect(trigger).toHaveText("全部语言");
  const formValues = () => page.locator("form").evaluate((form) =>
    Object.fromEntries(new FormData(form as HTMLFormElement)),
  );
  expect(await formValues()).toEqual({ language: "", default: "value:" });
  await page.getByText("受控语言", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await trigger.press("ArrowDown");
  await expect(page.getByRole("option", { name: "不可用" })).toBeDisabled();
  await page.keyboard.press("Home");
  await expect(page.getByRole("option", { name: "全部语言" })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("option", { name: "Alpha" })).toBeFocused();
  await expect(page.getByRole("option", { name: "Alpha" })).toHaveCSS("background-color", "rgb(244, 244, 245)");
  await page.keyboard.press("Enter");
  await expect(trigger).toHaveText("Alpha");
  await expect(trigger).toBeFocused();
  await trigger.press("Space");
  await page.keyboard.press("End");
  await expect(page.getByRole("option", { name: "项目 29", exact: true })).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(page.getByRole("option", { name: "项目 28", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(trigger).toHaveText("Alpha");
  await expect(trigger).toBeFocused();
  await trigger.press("Enter");
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("Home");
  await expect(page.getByRole("option", { name: "全部语言" })).toBeFocused();
  await page.keyboard.press("Space");
  await expect(trigger).toHaveText("全部语言");
  await trigger.press("a");
  await expect(trigger).toHaveText("Alpha");
  await chooseOption(trigger, "中文");
  expect(await formValues()).toEqual({ language: "中文", default: "value:" });
  await page.getByRole("button", { name: "Set controlled value" }).click();
  await expect(trigger).toHaveText("Alpha");
  await chooseOption(trigger, "全部语言");
  expect(await formValues()).toEqual({ language: "", default: "value:" });
  await chooseOption(page.getByLabel("Default value"), "Other");
  expect(await formValues()).toEqual({ language: "", default: "other" });
  await expect(page.getByLabel("Disabled select")).toBeDisabled();
  await expect(page.getByLabel("Disabled fieldset")).toBeDisabled();
  await trigger.click();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("listbox")).toBeHidden();
  await expect(page.getByRole("button", { name: "After select" })).toBeFocused();
  await trigger.click();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Set controlled value" })).toBeFocused();
});

test("long Chinese options wrap and scroll within a small window", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 360 });
  await page.goto("/tests/select-harness.html");
  await page.getByLabel("受控语言").click();
  const list = page.getByRole("listbox");
  const box = (await list.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(320);
  expect(box.y + box.height).toBeLessThanOrEqual(360);
  const long = page.getByRole("option", { name: /很长的中文/ });
  expect(await long.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.keyboard.press("End");
  await expect(page.getByRole("option", { name: "项目 29", exact: true })).toBeInViewport();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("受控语言")).toHaveText("项目 29");
});
