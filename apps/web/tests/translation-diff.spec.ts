import { expect, test, type Locator, type Page } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

async function openHistory(
  page: Page,
  before: string | null,
  after: string | null,
) {
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, target_lang: "zh" },
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: "Chapter One",
      segments: [
        { index: 0, source: "Source paragraph.", target: after, kind: "text" },
      ],
      review_issues: [],
    },
    [`/projects/${pid}/review/0/segments/0/history`]: [
      {
        id: "polish-1",
        kind: "polish",
        before,
        after,
        created_at: "2026-09-17T12:00:00Z",
      },
    ],
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByRole("button", { name: "Paragraph actions" }).click();
  await page.getByRole("menuitem", { name: "Change history", exact: true }).click();
  return page.getByRole("dialog", { name: "Paragraph editor", exact: true });
}

async function reconstructed(text: Locator) {
  return text.evaluate((element) => {
    const nodes = Array.from(element.children);
    return {
      before: nodes
        .filter((node) => node.tagName !== "INS")
        .map((node) => node.textContent)
        .join(""),
      after: nodes
        .filter((node) => node.tagName !== "DEL")
        .map((node) => node.textContent)
        .join(""),
    };
  });
}

test("Chinese changes use project language and reconstruct both versions exactly", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const Original = Intl.Segmenter;
    const locales: string[] = [];
    Object.defineProperty(window, "diffLocales", { value: locales });
    Object.defineProperty(Intl, "Segmenter", {
      value: class extends Original {
        constructor(locale: string, options: Intl.SegmenterOptions) {
          locales.push(locale);
          super(locale, options);
        }
      },
    });
  });
  const before = "他很快走进房间，说：“我回来了。”\nHello  world! 👩‍💻";
  const after = "他匆匆走进房间，低声说道：“我回来了！”\nHello world! 👩‍💻";
  const dialog = await openHistory(page, before, after);
  const text = dialog.getByTestId("translation-diff-text");
  await expect(text.locator("del").first()).toBeVisible();
  await expect(text.locator("ins").first()).toBeVisible();
  expect(await reconstructed(text)).toEqual({ before, after });
  expect(
    await page.evaluate(
      () => (window as unknown as { diffLocales: string[] }).diffLocales,
    ),
  ).toContain("zh-Hans");
  await dialog.getByRole("button", { name: "Full texts", exact: true }).click();
  const panels = dialog.getByTestId("translation-diff").locator("section p");
  expect(await panels.nth(0).textContent()).toBe(before);
  expect(await panels.nth(1).textContent()).toBe(after);
  await dialog
    .getByRole("button", { name: "Highlight changes", exact: true })
    .click();
  expect(await reconstructed(text)).toEqual({ before, after });
});

test("polishing compares its own snapshots, not subsequent manual edits", async ({
  page,
}) => {
  const before = "他很快走进房间。";
  const polished = "他匆匆走进房间。";
  const current = "这是后来人工修改的版本。";
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, target_lang: "zh" },
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: "Chapter One",
      segments: [
        {
          index: 0,
          source: "Source.",
          target: current,
          target_before_polish: before,
          kind: "text",
        },
      ],
      review_issues: [],
    },
    [`/projects/${pid}/review/0/segments/0/history`]: [
      {
        id: "manual-2",
        kind: "manual",
        before: polished,
        after: current,
        created_at: "2026-09-18T12:00:00Z",
      },
      {
        id: "polish-1",
        kind: "polish",
        before,
        after: polished,
        created_at: "2026-09-17T12:00:00Z",
      },
    ],
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByRole("button", { name: "Paragraph actions" }).click();
  await page.getByRole("menuitem", { name: "Change history", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const polish = dialog
    .locator("details")
    .filter({ has: page.getByText("Polishing", { exact: true }) });
  await expect(polish.getByTestId("translation-diff")).toHaveCount(0);
  await polish.locator("summary").click();
  expect(await reconstructed(polish.getByTestId("translation-diff-text"))).toEqual({
    before,
    after: polished,
  });
  await expect(dialog.getByText("Initial translation → current version")).toHaveCount(0);
  await polish.locator("summary").click();
  await expect(polish.getByTestId("translation-diff")).toHaveCount(0);
});

test("old projects label initial/current comparison without inventing a polish revision", async ({
  page,
}) => {
  const before = "初译文本。";
  const after = "当前文本，可能经过人工修改。";
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, target_lang: "zh" },
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: "Chapter One",
      segments: [
        {
          index: 0,
          source: "Source.",
          target: after,
          target_before_polish: before,
          kind: "text",
        },
      ],
      review_issues: [],
    },
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByRole("button", { name: "Paragraph actions" }).click();
  await page.getByRole("menuitem", { name: "Change history", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Initial translation → current version")).toBeVisible();
  await expect(dialog.getByText(/may include later manual edits/)).toBeVisible();
  await expect(dialog.getByText("Polishing", { exact: true })).toHaveCount(0);
  expect(await reconstructed(dialog.getByTestId("translation-diff-text"))).toEqual({
    before,
    after,
  });
});

test("mobile character fallback preserves emoji, whitespace and literal markup", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    Object.defineProperty(Intl, "Segmenter", { value: undefined });
  });
  const before = '你好👩‍💻  <script>alert("old")</script>\n下一行';
  const after = '您好👩‍💻 <script>alert("new")</script>\n下一行！';
  const dialog = await openHistory(page, before, after);
  const text = dialog.getByTestId("translation-diff-text");
  expect(await reconstructed(text)).toEqual({ before, after });
  await expect(text.locator("script")).toHaveCount(0);
  const spans = await text.locator("span, ins, del").allTextContents();
  expect(spans.every((value) => !/[\uD800-\uDFFF]/u.test(value))).toBe(true);
  expect(
    await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
});

for (const [name, before, after] of [
  ["unchanged", "相同文本。", "相同文本。"],
  ["intentionally empty", "", ""],
  ["insertion", "", "新增译文。"],
  ["deletion", "移除译文。", ""],
] as const) {
  test(`${name} versions retain empty-string semantics`, async ({ page }) => {
    const dialog = await openHistory(page, before, after);
    const text = dialog.getByTestId("translation-diff-text");
    if (before === after) {
      await expect(dialog.getByText("No text changes.", { exact: true })).toBeVisible();
      await expect(text).toHaveText(before || "(Empty translation)");
      await expect(text.locator("del, ins")).toHaveCount(0);
    } else {
      expect(await reconstructed(text)).toEqual({ before, after });
    }
  });
}

test("absent initial text is not treated as an empty translation", async ({ page }) => {
  const dialog = await openHistory(page, null, "First translation.");
  await expect(dialog.getByTestId("translation-diff")).toHaveCount(0);
  await expect(dialog.getByText("First translation.", { exact: true })).toBeVisible();
});

test("large comparisons fall back to complete texts without blocking the editor", async ({
  page,
}) => {
  const before = "原译文".repeat(4000);
  const after = "新译文".repeat(4000);
  const dialog = await openHistory(page, before, after);
  await expect(dialog.getByText(/too large or complex/)).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Full texts", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const highlight = dialog.getByRole("button", {
    name: "Highlight changes",
    exact: true,
  });
  await expect(highlight).toHaveAttribute("aria-pressed", "false");
  await expect(highlight).toBeDisabled();
  const panels = dialog.getByTestId("translation-diff").locator("section p");
  expect(await panels.nth(0).textContent()).toBe(before);
  expect(await panels.nth(1).textContent()).toBe(after);
  await expect(dialog.getByTestId("translation-diff-text")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Close", exact: true }).last().click();
  await expect(dialog).toHaveCount(0);
});

test("high-edit comparisons below the size limit also fall back safely", async ({
  page,
}) => {
  const before = "甲 ".repeat(500);
  const after = "乙 ".repeat(500);
  const dialog = await openHistory(page, before, after);
  await expect(dialog.getByText(/too large or complex/)).toBeVisible();
  const panels = dialog.getByTestId("translation-diff").locator("section p");
  expect(await panels.nth(0).textContent()).toBe(before);
  expect(await panels.nth(1).textContent()).toBe(after);
});
