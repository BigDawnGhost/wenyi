import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

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
      () => (window as Window & { copiedText: string[] }).copiedText,
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

test("the mobile editor fits long Chinese text and retains a single scroll area", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
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
    path: testInfo.outputPath("mobile-paragraph-editor.png"),
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
      () => (window as Window & { copiedText: string[] }).copiedText,
    ),
  ).toEqual(["原文第一段"]);
  await expect(page.getByRole("textbox")).toHaveCount(0);
});
