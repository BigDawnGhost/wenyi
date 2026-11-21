import { test, expect } from "@playwright/test";
import { fakeApi, mockChapter, pid, project, chapter } from "../../sandbox/fixtures";


/** Whether the Desktop host must keep running this suite as well.
 *
 * Chapter proofreading, draft conflicts and polling while paused, all driven through shared `/review` fixtures. The probe recorded only `preferences` and `request`.
 */
export const desktopHostSensitive = false;

export function registerTests() {
test("quote widths follow the writing system without changing book content", async ({ page }) => {
  const prose = [
    "«\u202fL’heure\u202f»", "„Привет“", "‘Ελληνικά’", "‘مرحبا’", "‘Paris — Москва’",
  ];
  const cjk = ["她说：“Hello。”", "「こんにちは」", "그는 ‘안녕’이라고 했다."];
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, { [`/projects/${pid}`]: { ...project, source_lang: "auto" } });
  await mockChapter(page, 0, () => ({
    index: 0, segments: [...prose, ...cjk].map((source, index) => ({ index, source, target: null, kind: "text" })),
  }));
  await page.goto(`/projects/${pid}/proofreading/0`);
  // Make inheritance observable without relying on installed CJK fonts.
  await page.addStyleTag({ content: 'html { font-family: monospace; }' });
  for (const text of [...prose, ...cjk]) {
    const paragraph = page.getByText(text, { exact: true });
    await expect(paragraph).toBeVisible();
    expect(await paragraph.textContent()).toBe(text);
    if (cjk.includes(text)) {
      await expect(paragraph).toHaveCSS("font-family", "monospace");
    } else {
      await expect(paragraph).toHaveCSS("font-family", /Wenyi Proportional Quotes/);
    }
  }
  // The quote face must not claim letters, combining marks or real spaces.
  const coverage = await page.evaluate(async () => {
    const font = '14px "Wenyi Proportional Quotes"';
    return [
      (await document.fonts.load(font, "’«")).length,
      (await document.fonts.load(font, "AéЖΩم\u0301 \u202f")).length,
    ];
  });
  expect(coverage).toEqual([1, 0]);
});

for (const sourceLanguage of ["en", "auto"]) {
test(`book text keeps proportional apostrophes in a Chinese interface (${sourceLanguage})`, async ({ page }) => {
  const source = "‘He’d go,’ she said.  ‘Wouldn’t you?’";
  const target = "« J’irais », dit-elle.";
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, { [`/projects/${pid}`]: { ...project, source_lang: sourceLanguage, target_lang: "fr" } });
  await mockChapter(page, 0, () => ({
    index: 0, segments: [
      { index: 0, kind: "text", source, target },
      { index: 1, kind: "text", source: "她说：“Hello。”", target: null },
    ],
  }));
  await page.goto(`/projects/${pid}/proofreading/0`);
  const paragraph = page.locator("#paragraph-0");
  const original = paragraph.locator('[data-reader-text="source"]');
  const translation = paragraph.getByTestId("translation-text");
  // Do not depend on the CI runner having a particular CJK system font:
  // simulate an inherited wide glyph and measure the apostrophe inside the word.
  await page.addStyleTag({ content: '[data-testid="chapter-reader"] { font-family: monospace; }' });
  await expect(original).toHaveAttribute("lang", sourceLanguage === "auto" ? "" : sourceLanguage);
  const advance = await original.evaluate(p => {
    const range = document.createRange();
    range.setStart(p.firstChild!, 3);
    range.setEnd(p.firstChild!, 4);
    return range.getBoundingClientRect().width / parseFloat(getComputedStyle(p).fontSize);
  });
  expect(advance).toBeLessThan(0.5);
  await expect(page.locator('#paragraph-1 [data-reader-text="source"]')).toHaveCSS("font-family", "monospace");
  await expect(translation).toHaveAttribute("lang", "fr");
  expect(await original.textContent()).toBe(source);
  await paragraph.getByRole("button", { name: "段落操作" }).click();
  await page.getByRole("menuitem", { name: "编辑译文" }).click();
  const editor = page.getByRole("dialog");
  await expect(editor.getByText(source, { exact: true })).toHaveAttribute("lang", sourceLanguage === "auto" ? "" : sourceLanguage);
  await expect(editor.getByRole("textbox")).toHaveAttribute("lang", "fr");
  await expect(editor.getByRole("textbox")).toHaveValue(target);
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
});
}

test("proofreading is separate and refreshes saved batches before a chapter finishes", async ({
  page,
}) => {
  let saved = false;
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "translating" },
  });
  await page.route(`**/api/projects/${pid}/chapters`, (route) =>
    route.fulfill({
      json: [
        {
          ...chapter,
          status: "translating",
          word_count: 2,
          target_word_count: saved ? 1 : 0,
        },
      ],
    }),
  );
  await mockChapter(page, 0, () => ({
        index: 0,
        title: chapter.title,
        segments: [
          {
            index: 12,
            source: "First source paragraph",
            target: saved ? "First saved batch" : null,
            kind: "text",
          },
          {
            index: 18,
            source: "Next source paragraph",
            target: null,
            kind: "text",
          },
        ],
        review_issues: [],
  }));
  await page.goto(`/projects/${pid}/review`);
  await expect(
    page.getByRole("heading", { name: "Proofread by chapter" }),
  ).toHaveCount(0);
  await page
    .getByRole("link", { name: "Manual proofreading", exact: true })
    .click();
  await expect(page).toHaveURL(`/projects/${pid}/proofreading`);
  await page.getByRole("link", { name: /Chapter One/ }).click();
  await expect(
    page.getByText("Waiting for translation", { exact: true }),
  ).toHaveCount(2);
  await expect(page.getByText(/^#\d+$/)).toHaveCount(0);
  saved = true;
  await expect(
    page.getByText("First saved batch", { exact: true }),
  ).toBeVisible({ timeout: 8000 });
  await page
    .getByText("First saved batch", { exact: true })
    .click({ button: "right" });
  await expect(
    page.getByRole("menuitem", { name: "Edit translation", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(
    page.getByText("Waiting for translation", { exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByText("1 / 2 paragraphs saved", { exact: true }),
  ).toBeVisible();
});

test("paused proofreading stops polling, preserves conflicting drafts and isolates chapters", async ({
  page,
}) => {
  await page.clock.install();
  let target = "Saved translation";
  let reads = 0;
  let edit: unknown;
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "paused" },
    [`/projects/${pid}/chapters`]: [
      {
        ...chapter,
        status: "translating",
        word_count: 2,
        target_word_count: 1,
      },
      { ...chapter, index: 2, title: "Chapter Three", status: "pending" },
    ],
    [`/projects/${pid}/review/2`]: {
      index: 2,
      title: "Chapter Three",
      review_issues: [],
      segments: [
        {
          index: 12,
          source: "Another chapter",
          target: "Independent translation",
          kind: "text",
        },
      ],
    },
  });
  await mockChapter(page, 0, () => {
    reads++;
    return {
        index: 0,
        title: chapter.title,
        review_issues: [],
        segments: [
          { index: 12, source: "First source", target, kind: "text" },
          { index: 18, source: "Pending source", target: null, kind: "text" },
        ],
    };
  });
  await page.route(`**/api/projects/${pid}/review/0/segments/12`, (route) => {
    edit = route.request().postDataJSON();
    if ((edit as { expected_target: string }).expected_target !== target)
      return route.fulfill({ status: 409, json: { detail: "Translation changed before saving" } });
    target = (edit as { target: string }).target;
    return route.fulfill({ json: { ok: true, index: 12 } });
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page
    .getByText("Saved translation", { exact: true })
    .click({ button: "right" });
  await page
    .getByRole("menuitem", { name: "Edit translation", exact: true })
    .click();
  await page.getByLabel("Edit translation").fill("Human draft");
  const before = reads;
  target = "Updated stored translation";
  await page.clock.runFor(60_000);
  expect(reads).toBe(before);
  await page.getByRole("button", { name: "Save translation" }).click();
  await expect.poll(() => reads).toBeGreaterThan(before);
  await expect(page.getByLabel("Edit translation")).toHaveValue("Human draft");
  await expect(
    page.getByRole("button", { name: "Save translation" }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Load latest translation", exact: true })
    .click();
  await expect(page.getByLabel("Edit translation")).toHaveValue(
    "Updated stored translation",
  );
  await page.getByLabel("Edit translation").fill("Human draft");
  await page.getByRole("button", { name: "Save translation" }).click();
  // Saving also refreshes chapter data before the editor closes.
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const savedTranslation = page
    .locator("#paragraph-12")
    .getByTestId("translation-text");
  await expect(savedTranslation).toHaveText("Human draft");
  expect(edit).toEqual({
    target: "Human draft",
    expected_target: "Updated stored translation",
  });
  await savedTranslation.click({ button: "right" });
  await page
    .getByRole("menuitem", { name: "Edit translation", exact: true })
    .click();
  await page.getByLabel("Edit translation").fill("Unsaved chapter draft");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Close", exact: true })
    .last()
    .click();
  await page.getByRole("link", { name: "Next chapter" }).click();
  await expect(page).toHaveURL(`/projects/${pid}/proofreading/2`);
  await expect(
    page.getByText("Independent translation", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Edit translation")).toHaveCount(0);
});

test("Chinese desktop navigation opens proofreading and distinguishes saved empty translations", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, {
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: chapter.title,
      review_issues: [],
      segments: [
        { index: 0, source: "Parser noise", target: "", kind: "text" },
      ],
    },
  });
  await page.goto(`/projects/${pid}/review`);
  await page.getByRole("link", { name: "人工校阅", exact: true }).click();
  await expect(page.getByRole("heading", { name: "人工校阅", exact: true })).toBeVisible();
  await expect(page.getByText("人工逐章校阅", { exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: /Chapter One/ }).click();
  await expect(page.getByText("等待译文落盘", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText("已保存 1 / 1 段", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "段落操作", exact: true }).click();
  await expect(
    page.getByRole("menuitem", { name: "编辑译文", exact: true }),
  ).toBeEnabled();
});
}
