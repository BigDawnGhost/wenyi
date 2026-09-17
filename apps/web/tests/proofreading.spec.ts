import { test, expect } from "@playwright/test";
import { fakeApi, pid, project, chapter } from "./fixtures";

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
  await page.route(`**/api/projects/${pid}/review/0`, (route) =>
    route.fulfill({
      json: {
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
      },
    }),
  );
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

test("paused partial proofreading preserves drafts while polling and isolates chapters", async ({
  page,
}) => {
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
  await page.route(`**/api/projects/${pid}/review/0`, (route) => {
    reads++;
    return route.fulfill({
      json: {
        index: 0,
        title: chapter.title,
        review_issues: [],
        segments: [
          { index: 12, source: "First source", target, kind: "text" },
          { index: 18, source: "Pending source", target: null, kind: "text" },
        ],
      },
    });
  });
  await page.route(`**/api/projects/${pid}/review/0/segments/12`, (route) => {
    edit = route.request().postDataJSON();
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
  await expect.poll(() => reads, { timeout: 8000 }).toBeGreaterThan(before);
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

test("mobile Chinese navigation opens proofreading and distinguishes saved empty translations", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
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
