import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

test("collapsed chapter review notes mount only after expansion", async ({ page }) => {
  await fakeApi(page, {
    [`/projects/${pid}/review/0`]: {
      index: 0, title: "Chapter One",
      segments: [{ index: 0, source: "Source", target: "Translation", kind: "text" }],
      review_issues: Array.from({ length: 1000 }, (_, index) => ({
        index, detail: `Deferred review note ${index}`,
      })),
    },
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect(page.getByTestId("translation-text")).toHaveText("Translation");
  const note = page.getByText("Deferred review note 999", { exact: true });
  await expect(note).toHaveCount(0);
  const summary = page.locator("summary").filter({ hasText: "Recorded review notes" });
  await summary.click();
  await expect(note).toHaveCount(1);
  await summary.click();
  await expect(note).toHaveCount(1);
});

test("one shared paragraph menu follows the active row and restores keyboard focus", async ({ page }) => {
  await fakeApi(page, {
    [`/projects/${pid}/review/0`]: {
      index: 0, title: "Chapter One", review_issues: [],
      segments: [
        { index: 12, source: "First source", target: "First translation", kind: "text" },
        { index: 18, source: "Second source", target: "", kind: "text" },
      ],
    },
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  const first = page.locator("#paragraph-12").getByRole("button");
  const second = page.locator("#paragraph-18").getByRole("button");
  await first.click();
  await expect(first).toHaveAttribute("aria-expanded", "true");
  await page.locator("#paragraph-18").getByTestId("translation-text")
    .click({ button: "right", position: { x: 20, y: 20 } });
  await expect(page.getByRole("menu")).toHaveCount(1);
  await expect(first).toHaveAttribute("aria-expanded", "false");
  await expect(second).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("End");
  await expect(page.getByRole("menuitem", { name: "Copy source", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(second).toBeFocused();
  await page.keyboard.press("Shift+F10");
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  await expect(page.getByRole("dialog").getByText("Second source", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Edit translation", { exact: true })).toHaveValue("");
  await page.keyboard.press("Escape");
  await expect(second).toBeFocused();
});

test("paragraph menus dismiss outside without registering global pointer listeners", async ({ page }) => {
  await page.addInitScript(() => {
    const registrations: string[] = [];
    (window as any).__pointerRegistrations = registrations;
    const add = window.addEventListener;
    window.addEventListener = function (
      type: string,
      listener: EventListenerOrEventListenerObject,
      options?: boolean | AddEventListenerOptions,
    ) {
      if (type.startsWith("pointer") || type.startsWith("touch")) registrations.push(type);
      return add.call(this, type, listener, options);
    };
  });
  await fakeApi(page);
  await page.goto(`/projects/${pid}/proofreading/0`);
  const trigger = page.getByRole("button", { name: "Paragraph actions", exact: true });
  await expect(trigger).toBeVisible();
  const before = await page.evaluate(() => (window as any).__pointerRegistrations.length);
  await trigger.click();
  await expect(page.getByRole("menu")).toBeVisible();
  expect(await page.evaluate(() => (window as any).__pointerRegistrations.length)).toBe(before);
  const heading = page.getByRole("heading", { name: /Manual proofreading/ });
  await heading.click();
  await expect(page.getByRole("menu")).toHaveCount(0);
  await trigger.click();
  await expect(page.getByRole("menu")).toBeVisible();
  await heading.click({ button: "right" });
  await expect(page.getByRole("menu")).toHaveCount(0);
});

test("editor autosizing survives opening, resizing and tab changes without observer errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    (window as any).__resizeErrors = [];
    window.addEventListener("error", (event) => {
      if (event.message.includes("ResizeObserver")) (window as any).__resizeErrors.push(event.message);
    });
  });
  const text = "A long translated sentence. 较长的中文译文。\n".repeat(80);
  await fakeApi(page, {
    [`/projects/${pid}/review/0`]: {
      index: 0, title: "Chapter One", review_issues: [],
      segments: [{ index: 0, kind: "text", source: "Synthetic source.", target: text }],
    },
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByRole("button", { name: "Paragraph actions", exact: true }).click();
  await page.getByRole("menuitem", { name: "Edit translation", exact: true }).click();
  const input = page.getByLabel("Edit translation", { exact: true });
  const dialog = page.getByRole("dialog");
  await expect(input).toBeFocused();
  for (const width of [1440, 1024, 1280]) {
    await page.setViewportSize({ width, height: 850 });
    await expect.poll(() => input.evaluate((element) => element.scrollHeight <= element.clientHeight + 2)).toBe(true);
    await expect(dialog.getByRole("button", { name: "Save translation", exact: true })).toBeInViewport();
  }
  await input.fill("Short draft");
  await expect.poll(() => input.evaluate((element) => element.clientHeight)).toBeLessThan(300);
  await dialog.getByRole("tab", { name: "Change history", exact: true }).click();
  await dialog.getByRole("tab", { name: "Translation", exact: true }).click();
  await expect(input).toHaveValue("Short draft");
  await input.fill(text);
  await expect.poll(() => input.evaluate((element) => element.scrollHeight <= element.clientHeight + 2)).toBe(true);
  await page.keyboard.press("Escape");
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(await page.evaluate(() => (window as any).__resizeErrors)).toEqual([]);
  expect(errors).toEqual([]);
});

test("reading and selection do not edit; the context menu copies the selected text", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const copied: string[] = [];
    Object.defineProperty(window, "copiedText", { value: copied });
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async (text: string) => {
          copied.push(text);
        },
      },
    });
  });
  await fakeApi(page);
  await page.goto(`/projects/${pid}/proofreading/0`);
  const translation = page.getByTestId("translation-text");
  await translation.click();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await translation.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
  });
  await translation.click({ button: "right" });
  await page
    .getByRole("menuitem", { name: "Copy selection", exact: true })
    .click();
  await expect(page.getByText("Copied", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => (window as unknown as { copiedText: string[] }).copiedText,
    ),
  ).toEqual(["Original translation"]);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Paragraph actions", exact: true })
    .focus();
  await page.keyboard.press("Shift+F10");
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
});

test("the editor expands long text and combines polishing and manual history", async ({
  page,
}, testInfo) => {
  const longText = Array.from(
    { length: 36 },
    (_, i) =>
      `Line ${i + 1}: a translated paragraph with enough text to read and edit.`,
  ).join("\n");
  let writes = 0;
  await fakeApi(page, {
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: "Chapter One",
      segments: [
        {
          index: 0,
          source: "Source paragraph for comparison.",
          target: longText,
          target_before_polish: "Rough draft",
          kind: "text",
        },
      ],
      review_issues: [],
    },
    [`/projects/${pid}/review/0/segments/0/history`]: [
      {
        id: "3",
        kind: "manual",
        before: "Polished version",
        after: longText,
        created_at: "2026-09-17T13:00:00Z",
      },
      {
        id: "2",
        kind: "polish",
        before: "Rough draft",
        after: "Polished version",
        created_at: "2026-09-17T12:00:00Z",
      },
      {
        id: "1",
        kind: "translation",
        before: null,
        after: "Rough draft",
        created_at: "2026-09-17T12:00:00Z",
      },
    ],
  });
  await page.route(
    `**/api/projects/${pid}/review/0/segments/0`,
    async (route) => {
      writes++;
      await route.fulfill({ json: { ok: true } });
    },
  );
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect(
    page.getByText("Translation before polishing", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByTestId("translation-text")
    .click({ button: "right", position: { x: 20, y: 20 } });
  await page
    .getByRole("menuitem", { name: "Edit translation", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Paragraph editor",
    exact: true,
  });
  await expect(
    dialog.getByText("Source paragraph for comparison.", { exact: true }),
  ).toBeVisible();
  const input = page.getByLabel("Edit translation", { exact: true });
  await expect(input).toHaveValue(longText);
  expect(
    await input.evaluate(
      (element) => element.scrollHeight <= element.clientHeight + 2,
    ),
  ).toBe(true);
  await expect(
    dialog.getByRole("button", { name: "Save translation", exact: true }),
  ).toBeInViewport();
  await input.fill("My unsaved draft");
  await dialog
    .getByRole("tab", { name: "Change history", exact: true })
    .click();
  await expect(dialog.getByText("Manual edit", { exact: true })).toBeVisible();
  await dialog.locator("summary").filter({ hasText: "Polishing" }).click();
  await dialog
    .locator("details")
    .filter({ has: page.locator("summary", { hasText: "Polishing" }) })
    .getByRole("button", { name: "Full texts", exact: true })
    .click();
  await expect(
    dialog.getByText("Rough draft", { exact: true }).first(),
  ).toBeVisible();
  await dialog
    .locator("details")
    .filter({ has: page.locator("summary", { hasText: "Polishing" }) })
    .getByRole("button", { name: "Use this version as a draft" })
    .click();
  await expect(input).toHaveValue("Polished version");
  expect(writes).toBe(0);
  await page.screenshot({
    path: testInfo.outputPath("paragraph-editor.png"),
    fullPage: true,
  });
});

test("the desktop editor fits long Chinese text and retains a single scroll area", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1024, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  const text = "在宁静的清晨，河流穿过树林。".repeat(70);
  await fakeApi(page, {
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: "Chapter One",
      segments: [
        {
          index: 0,
          source: "A morning by the river.",
          target: text,
          kind: "text",
        },
      ],
      review_issues: [],
    },
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByRole("button", { name: "段落操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "编辑译文", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "段落编辑", exact: true });
  const editor = page.getByLabel("编辑译文", { exact: true });
  await expect(editor).toHaveValue(text);
  expect(
    await editor.evaluate(
      (element) => element.scrollHeight <= element.clientHeight + 2,
    ),
  ).toBe(true);
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
  await expect(
    dialog.getByRole("button", { name: "保存译文", exact: true }),
  ).toBeInViewport();
  await page.screenshot({
    path: testInfo.outputPath("compact-paragraph-editor.png"),
    fullPage: true,
  });
});

test("copy source works on HTTP deployments without the async clipboard API", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const copied: string[] = [];
    Object.defineProperty(window, "copiedText", { value: copied });
    Object.defineProperty(navigator, "clipboard", { value: undefined });
    document.execCommand = (command: string) => {
      if (command !== "copy") return false;
      copied.push((document.activeElement as HTMLTextAreaElement).value);
      return true;
    };
  });
  await fakeApi(page);
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByTestId("translation-text").click({ button: "right" });
  await page
    .getByRole("menuitem", { name: "Copy source", exact: true })
    .click();
  await expect(page.getByText("Copied", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => (window as unknown as { copiedText: string[] }).copiedText,
    ),
  ).toEqual(["原文第一段"]);
  await expect(page.getByRole("textbox")).toHaveCount(0);
});
