import { expect, test } from "@playwright/test";
import { fakeApi, project } from "./fixtures";

for (const chinese of [false, true]) {
  test(`creation is a modal over the retained dashboard in ${chinese ? "Chinese" : "English"}`, async ({ page }) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
    await fakeApi(page, { "/projects": [project] });
    let posts = 0;
    await page.route("**/api/projects", async route => {
      if (route.request().method() === "POST") posts++;
      await route.fallback();
    });
    await page.goto("/");
    const trigger = page.locator("#create-project-trigger");
    const name = chinese ? "创建项目" : "Create project";
    const headingName = chinese ? "我的项目" : "My projects";
    const dialog = page.getByRole("dialog", { name, exact: true });
    const input = page.getByLabel(chinese ? "项目名称" : "Project name", { exact: true });
    const dashboardHeading = page.getByRole("heading", { name: headingName, exact: true, includeHidden: true });
    const card = page.getByRole("link", { name: `${chinese ? "打开项目" : "Open project"} ${project.name}`, exact: true, includeHidden: true });
    await expect(card).toBeVisible();
    for (const method of ["cancel", "close", "backdrop", "escape", "back"] as const) {
      await page.evaluate(headingName => {
        const watch = { running: true, blankFrames: 0 };
        Object.assign(window, { dashboardWatch: watch });
        const sample = () => {
          const heading = [...document.querySelectorAll("h1")].find(node => node.textContent === headingName);
          if (!heading?.getClientRects().length) watch.blankFrames++;
          if (watch.running) requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
      }, headingName);
      await trigger.click();
      await expect(page).toHaveURL("/projects/new");
      await expect(dialog).toBeVisible();
      await expect(input).toBeFocused();
      await expect(input).toHaveValue("");
      await expect(dashboardHeading).toBeVisible();
      await expect(card).toBeVisible();
      await expect(page.getByRole("heading", { name: headingName, exact: true })).toHaveCount(0);
      await expect(page.getByRole("complementary", { includeHidden: true })).toHaveCount(0);
      expect(await page.evaluate(() => {
        const watch = (window as unknown as { dashboardWatch: { running: boolean; blankFrames: number } }).dashboardWatch;
        watch.running = false;
        return watch.blankFrames;
      })).toBe(0);
      await input.fill("Unsaved fixture");
      await page.getByLabel(chinese ? "上传原文" : "Upload source", { exact: true }).setInputFiles({
        name: "fixture.docx", mimeType: "application/octet-stream", buffer: Buffer.from("temporary source"),
      });
      if (method === "cancel") await dialog.getByRole("button", { name: chinese ? "取消" : "Cancel", exact: true }).click();
      if (method === "close") await dialog.getByRole("button", { name: chinese ? "关闭" : "Close", exact: true }).click();
      if (method === "backdrop") await page.mouse.click(2, 2);
      if (method === "escape") await page.keyboard.press("Escape");
      if (method === "back") await page.goBack();
      await expect(page).toHaveURL("/");
      await expect(dialog).toHaveCount(0);
      await expect(trigger).toBeFocused();
      expect(posts).toBe(0);
    }
    await trigger.click();
    await expect(input).toHaveValue("");
    await expect(dialog.getByText("fixture.docx", { exact: true })).toHaveCount(0);
  });
}

test("loading the creation module never blanks the dashboard", async ({ page }) => {
  await fakeApi(page, { "/projects": [project] });
  let release = () => {};
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/features/project-create/CreateProject.tsx*", async route => {
    await gate;
    await route.continue();
  });
  try {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "My projects", exact: true })).toBeVisible();
    const requested = page.waitForRequest("**/features/project-create/CreateProject.tsx*");
    await page.locator("#create-project-trigger").click();
    await requested;
    const blankFrames = await page.evaluate(async () => {
      let blank = 0;
      for (let frame = 0; frame < 45; frame++) {
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
        if (!document.querySelector("h1")?.getClientRects().length ||
            document.querySelector("[data-route-pending]")) blank++;
      }
      return blank;
    });
    expect(blankFrames).toBe(0);
    release();
    await expect(page.getByRole("dialog", { name: "Create project", exact: true })).toBeVisible();
  } finally {
    release();
  }
});

test("closing creation preserves the history entry before the project list", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/settings");
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await page.locator("#create-project-trigger").click();
  await page.getByRole("dialog", { name: "Create project", exact: true })
    .getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page).toHaveURL("/");
  await page.goBack();
  await expect(page).toHaveURL("/settings");
});

test("direct creation route retains the dashboard and dropdown Escape does not dismiss the dialog", async ({ page }) => {
  await fakeApi(page, { "/projects": [project] });
  await page.goto("/projects/new");
  const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("heading", { name: "My projects", exact: true, includeHidden: true })).toBeVisible();
  await expect(page.getByLabel("Project name", { exact: true })).toBeFocused();
  const language = dialog.getByLabel("Target language", { exact: true });
  await language.click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await expect(page.getByRole("option", { name: "English (en)", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(language).toBeFocused();
  await language.click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("Tab");
  expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await expect(page.getByRole("listbox")).toHaveCount(0);
  for (const key of ["Tab", "Shift+Tab"]) {
    for (let index = 0; index < 18; index++) {
      await page.keyboard.press(key);
      expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
    }
  }
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL("/");
  await expect(page.locator("#create-project-trigger")).toBeFocused();
});

test("320px dialog scrolls its form without losing the close header", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 480 });
  await fakeApi(page);
  await page.goto("/projects/new");
  const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
  const close = dialog.getByRole("button", { name: "Close", exact: true });
  await expect(close).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  const scrolled = await dialog.evaluate(element => {
    const body = [element, ...element.querySelectorAll<HTMLElement>("*")].find(node =>
      node.scrollHeight > node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY));
    if (!body) return false;
    body.scrollTop = body.scrollHeight;
    return body.scrollTop > 0;
  });
  expect(scrolled).toBe(true);
  await expect(dialog.getByRole("button", { name: "Create project", exact: true })).toBeInViewport();
  await expect(close).toBeInViewport();
  await close.click();
  await expect(page).toHaveURL("/");
});
