import { expect, test } from "@playwright/test";
import { fakeApi, project } from "./fixtures";

for (const chinese of [false, true]) {
  for (const populated of [false, true]) {
    test(`${chinese ? "Chinese" : "English"} ${populated ? "populated" : "empty"} dashboard opens standalone settings`, async ({ page }) => {
      await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
      await fakeApi(page, { "/projects": populated ? [project] : [] });
      await page.goto("/");
      await expect(page.getByRole("heading", { name: chinese ? "我的项目" : "My projects", exact: true })).toBeVisible();
      await expect(page.getByRole("complementary")).toHaveCount(1);
      const shell = await page.locator("#sidebar-navigation").elementHandle();
      await page.getByRole("link", { name: chinese ? "设置" : "Settings", exact: true }).press("Enter");
      await expect(page).toHaveURL("/settings");
      expect(await shell!.evaluate(node => node === document.querySelector("#sidebar-navigation"))).toBe(true);
      await expect(page.getByLabel(chinese ? "界面语言" : "Interface language", { exact: true })).toBeVisible();
      await expect(page.getByRole("navigation", { name: chinese ? "设置导航" : "Settings navigation" })).toBeVisible();
      await page.getByRole("button", { name: chinese ? "返回" : "Back", exact: true }).click();
      await expect(page).toHaveURL("/");
      if (!populated) await expect(page.getByText(chinese ? "还没有项目。" : "No projects yet.", { exact: true })).toBeVisible();
      await page.locator("#create-project-trigger").press("Enter");
      await expect(page).toHaveURL("/projects/new");
      await expect(page.getByRole("dialog", { name: chinese ? "创建项目" : "Create project", exact: true })).toBeVisible();
      await expect(page.getByRole("complementary", { includeHidden: true })).toHaveCount(1);
    });
  }
}

test("settings category links survive reload and unknown categories fall back to General", async ({ page }) => {
  await fakeApi(page);
  for (const [path, title] of [
    ["providers", "Models & connections"], ["defaults", "Default models"], ["advanced", "Advanced"],
    ["interface", "General"], ["not-a-category", "General"],
  ]) {
    await page.goto(`/settings/${path}`);
    const destination = title === "General" ? "/settings" : `/settings/${path}`;
    await expect(page).toHaveURL(destination);
    await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Settings navigation" })).toBeVisible();
    await page.reload();
    await expect(page).toHaveURL(destination);
  }
});

test("structured, registry ID and YAML drafts survive category switches", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/settings/workflow");
  await page.getByLabel("Polishing", { exact: true }).uncheck();
  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await page.getByLabel("Model ID", { exact: true }).fill("pending model");
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
  await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toBeDisabled();
  await page.getByRole("link", { name: "General", exact: true }).click();
  await expect(page).toHaveURL("/settings");
  await page.getByRole("link", { name: "Apply or discard the ID edits before saving.", exact: true }).click();
  await expect(page).toHaveURL("/settings/providers");
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveValue("pending model");
  await page.getByLabel("Model ID", { exact: true }).fill("pending_model");
  await page.getByLabel("Model ID", { exact: true }).press("Enter");
  await page.getByRole("link", { name: "Advanced", exact: true }).click();
  const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration", exact: true });
  await expect(yaml).toBeVisible();
  const draft = `${await yaml.inputValue()}\n# unsaved draft`;
  await yaml.fill(draft);
  await page.getByRole("link", { name: "Workflow", exact: true }).click();
  await expect(page.getByLabel("Polishing", { exact: true })).toBeDisabled();
  await page.getByRole("link", { name: /Advanced YAML has unvalidated changes/ }).click();
  await expect(page).toHaveURL("/settings/advanced");
  await expect(yaml).toHaveValue(draft);
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveValue("pending_model");
});

test("compact desktop dashboard cards and settings categories remain scrollable without overflow", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 640 });
  await fakeApi(page, { "/projects": Array.from({ length: 12 }, (_, index) => ({ ...project, id: `book-${index}`, name: `Book ${index}` })) });
  await page.goto("/");
  const last = page.getByRole("link", { name: "Open project Book 11", exact: true });
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  await page.goto("/settings");
  for (const [category, title] of [
    ["Advanced", "Advanced YAML configuration"], ["Workflow", "Segmentation and performance"],
  ]) {
    await page.getByRole("link", { name: category, exact: true }).click();
    await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect.poll(() => page.getByRole("main").evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  }
});

test("history navigation dismisses dropdowns without discarding same-page drafts", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/settings/workflow");
  await page.getByLabel("Polishing", { exact: true }).uncheck();
  await page.getByRole("link", { name: "Models & connections", exact: true }).click();
  await page.getByLabel("Model ID", { exact: true }).fill("pending model");
  await page.getByRole("link", { name: "General", exact: true }).click();
  await expect(page).toHaveURL("/settings");
  await expect(page.getByLabel("Interface language", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Apply or discard the ID edits before saving.", exact: true }).click();
  await expect(page.getByRole("link", { name: "Models & connections", exact: true })).toHaveAttribute("aria-current", "page");
  await page.locator("#provider-models").getByLabel("API provider", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL("/settings");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
  await page.getByLabel("Interface language", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL("/settings/providers");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveValue("pending model");
});
