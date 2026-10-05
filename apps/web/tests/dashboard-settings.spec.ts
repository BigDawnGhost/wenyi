import { expect, test } from "@playwright/test";
import { fakeApi, project } from "./fixtures";

for (const chinese of [false, true]) {
  for (const populated of [false, true]) {
    test(`${chinese ? "Chinese" : "English"} ${populated ? "populated" : "empty"} dashboard has only floating entries`, async ({ page }) => {
      await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
      await fakeApi(page, { "/projects": populated ? [project] : [] });
      await page.goto("/");
      await expect(page.getByRole("heading", { name: chinese ? "我的项目" : "My projects", exact: true })).toBeVisible();
      await expect(page.getByRole("complementary")).toHaveCount(0);
      await expect(page.getByRole("button", { name: /Collapse sidebar|Expand sidebar|收起侧边栏|展开侧边栏/ })).toHaveCount(0);
      const nav = page.getByRole("navigation", { name: chinese ? "全局导航" : "Global navigation" });
      await expect(nav).toHaveCSS("position", "fixed");
      await expect(nav.getByRole("link").first()).toHaveAttribute(
        "aria-label", chinese ? "创建项目" : "Create project",
      );
      const createBox = (await nav.getByRole("link", { name: chinese ? "创建项目" : "Create project", exact: true }).boundingBox())!;
      const settingsBox = (await nav.getByRole("link", { name: chinese ? "设置" : "Settings", exact: true }).boundingBox())!;
      expect(createBox.y + createBox.height).toBeLessThan(settingsBox.y);
      for (const name of [chinese ? "设置" : "Settings", chinese ? "创建项目" : "Create project"]) {
        const entry = page.getByRole("link", { name, exact: true });
        await expect(entry).toHaveCount(1);
        await expect(entry).toBeInViewport();
        await expect(entry).toHaveAttribute("title", name);
        await expect(entry).toHaveText("");
      }
      await expect(page.getByRole("button", { name: chinese ? "创建项目" : "Create project", exact: true })).toHaveCount(0);
      if (!populated) await expect(page.getByText(chinese ? "还没有项目。" : "No projects yet.", { exact: true })).toBeVisible();
      await nav.getByRole("link", { name: chinese ? "设置" : "Settings", exact: true }).focus();
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL("/settings");
      const sidebar = page.getByRole("complementary");
      await expect(sidebar.getByText(chinese ? "设置" : "Settings", { exact: true })).toHaveCount(0);
      const footer = sidebar.getByRole("navigation", { name: chinese ? "全局导航" : "Global navigation" });
      await expect(footer.getByRole("link")).toHaveCount(1);
      const back = footer.getByRole("link", { name: chinese ? "项目列表" : "Projects", exact: true });
      const sidebarBox = (await sidebar.boundingBox())!;
      const backBox = (await back.boundingBox())!;
      expect(sidebarBox.y + sidebarBox.height - backBox.y - backBox.height).toBeLessThanOrEqual(16);
      await back.click();
      await expect(page).toHaveURL("/");
      await page.getByRole("link", { name: chinese ? "创建项目" : "Create project", exact: true }).focus();
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL("/projects/new");
    });
  }
}

test("settings categories support deep links, history, reload and unknown routes", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.sidebarCollapsed", "true"));
  await fakeApi(page);
  await page.goto("/settings/providers");
  const nav = page.getByRole("navigation", { name: "Settings navigation" });
  const categories = [
    ["API providers & models", "/settings/providers"],
    ["New project defaults", "/settings/defaults"],
    ["Advanced YAML configuration", "/settings/advanced"],
    ["Interface language", "/settings"],
  ];
  await expect(page.getByRole("complementary")).toHaveCSS("width", "240px");
  await expect(page.getByRole("button", { name: /sidebar/ })).toHaveCount(0);
  for (const [name, path] of categories) {
    await nav.getByRole("link", { name, exact: true }).click();
    await expect(page).toHaveURL(path);
    await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);
    await expect(nav.getByRole("link", { name, exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: "Settings", exact: true, level: 1 })).toBeVisible();
    await page.reload();
    await expect(nav.getByRole("link", { name, exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toHaveCount(path === "/settings" ? 0 : 1);
  }
  await page.goBack();
  await expect(page).toHaveURL("/settings/advanced");
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await expect(page).toHaveURL("/");
  await page.goto("/settings/not-a-category");
  await expect(page).toHaveURL("/settings");
  await page.goto("/settings/interface");
  await expect(page).toHaveURL("/settings");
  await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);
});

test("history navigation dismisses dropdowns without discarding category drafts", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/settings/defaults");
  await page.getByLabel("Polishing", { exact: true }).uncheck();
  const nav = page.getByRole("navigation", { name: "Settings navigation" });
  await nav.getByRole("link", { name: "API providers & models", exact: true }).click();
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  await page.getByLabel("API provider", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL("/settings/defaults");
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
  await page.getByLabel("Quality tier", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();

  await page.goForward();
  await expect(page).toHaveURL("/settings/providers");
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);
  await expect(page.getByLabel("API provider", { exact: true })).toHaveText("deepseek");
  await nav.getByRole("link", { name: "Interface language", exact: true }).click();
  await page.getByLabel("Interface language", { exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL("/settings/providers");
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);
  await nav.getByRole("link", { name: "New project defaults", exact: true }).click();
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
});

test("mobile final cards scroll clear of floating controls and settings do not overflow", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await fakeApi(page, { "/projects": Array.from({ length: 12 }, (_, index) => ({ ...project, id: `book-${index}`, name: `Book ${index}` })) });
  await page.goto("/");
  const last = page.getByRole("link", { name: "Open project Book 11", exact: true });
  await last.scrollIntoViewIfNeeded();
  await page.getByRole("main").evaluate(node => { node.scrollTop = node.scrollHeight; });
  await expect(last).toBeInViewport();
  const card = (await last.boundingBox())!;
  const controls = (await page.getByRole("navigation", { name: "Global navigation" }).boundingBox())!;
  expect(card.y + card.height).toBeLessThan(controls.y);
  for (const path of ["/", "/settings", "/settings/providers", "/settings/defaults", "/settings/advanced"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    if (path === "/settings/providers") await page.locator("summary").filter({ hasText: "API providers & models" }).click();
    if (path === "/settings/advanced") await page.locator("summary").filter({ hasText: "Advanced YAML configuration" }).click();
    if (path === "/settings/defaults") await page.locator("summary").filter({ hasText: "Segmentation and performance" }).click();
    if (path.startsWith("/settings")) {
      const sidebar = page.getByRole("complementary");
      const categoriesBox = (await page.getByRole("navigation", { name: "Settings navigation" }).boundingBox())!;
      const backBox = (await sidebar.getByRole("link", { name: "Projects", exact: true }).boundingBox())!;
      expect(backBox.y).toBeGreaterThanOrEqual(categoriesBox.y + categoriesBox.height);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), path).toBe(true);
    await expect.poll(async () => page.getByRole("main").evaluate(node => node.scrollWidth <= node.clientWidth), { message: path }).toBe(true);
  }
});

test("structured, registry ID and YAML drafts survive category switching", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/settings/defaults");
  const nav = page.getByRole("navigation", { name: "Settings navigation" });
  const category = (name: string) => nav.getByRole("link", { name, exact: true }).click();
  await page.getByLabel("Polishing", { exact: true }).uncheck();
  await category("API providers & models");
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  await page.getByLabel("Model ID", { exact: true }).fill("pending model");
  await category("New project defaults");
  await expect(page.getByLabel("Polishing", { exact: true })).not.toBeChecked();
  await expect(page.getByRole("button", { name: "Save configuration", exact: true })).toBeDisabled();
  await page.getByRole("link", { name: "Apply or discard the ID edits before saving.", exact: true }).click();
  await expect(page).toHaveURL("/settings/providers");
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveValue("pending model");
  await page.getByLabel("Model ID", { exact: true }).fill("pending_model");
  await page.getByLabel("Model ID", { exact: true }).press("Enter");
  await category("Advanced YAML configuration");
  await page.locator("summary").filter({ hasText: "Advanced YAML configuration" }).click();
  const yaml = page.getByLabel("Advanced YAML configuration", { exact: true });
  const draft = `${await yaml.inputValue()}\n# unsaved category draft`;
  await yaml.fill(draft);
  await category("Interface language");
  await category("New project defaults");
  await expect(page.getByLabel("Polishing", { exact: true })).toBeDisabled();
  await page.getByRole("link", { name: /Advanced YAML has unvalidated changes/ }).click();
  await expect(page).toHaveURL("/settings/advanced");
  await expect(yaml).toHaveValue(draft);
  await category("API providers & models");
  await expect(page.getByLabel("Model ID", { exact: true })).toHaveValue("pending_model");
});
