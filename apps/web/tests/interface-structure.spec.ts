import { test, expect } from "@playwright/test";
import { fakeApi, pid, chapter, project } from "./fixtures";

test("chapter rows localize pending status and support search and filtering", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, {
    [`/projects/${pid}/chapters`]: [
      {
        ...chapter,
        title: "First chapter",
        status: "pending",
        target_word_count: 0,
      },
      { ...chapter, index: 1, title: "Second chapter", status: "done" },
    ],
  });
  await page.goto(`/projects/${pid}/proofreading`);
  await expect(
    page.getByRole("list").getByText("待翻译", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("pending", { exact: true })).toHaveCount(0);
  const list = page.getByRole("list", { name: "章节列表" });
  await expect(list.getByRole("listitem")).toHaveCount(2);
  await page.getByLabel("搜索章节").fill("Second");
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await page.getByLabel("搜索章节").fill("");
  await page.getByLabel("翻译状态筛选").selectOption("pending");
  await expect(list).toContainText("First chapter");
  await expect(list).not.toContainText("Second chapter");
});

test("review issues use searchable rows and preserve complete evidence on demand", async ({
  page,
}) => {
  const run = {
    id: "review-a",
    status: "completed",
    summary: { issue_count: 2 },
    issues: [
      {
        issue_id: "a",
        detail: "A missing phrase",
        evidence: { source: "Exact source evidence", status: "pending" },
      },
      {
        issue_id: "b",
        detail: "Inconsistent character name",
        evidence: { source: "Other evidence" },
      },
    ],
    changes: [],
    autofix: {},
    items: [
      {
        id: "issue:a",
        kind: "issue",
        status: "pending",
        detail: "A missing phrase",
        issue: {
          evidence: { source: "Exact source evidence", status: "pending" },
        },
        evidence: [],
        changes: [],
        publications: [],
      },
      {
        id: "issue:b",
        kind: "issue",
        status: "pending",
        detail: "Inconsistent character name",
        issue: { evidence: { source: "Other evidence" } },
        evidence: [],
        changes: [],
        publications: [],
      },
    ],
  };
  await fakeApi(page, {
    [`/projects/${pid}/review/runs`]: [run],
    [`/projects/${pid}/review/runs/review-a`]: run,
  });
  await page.goto(`/projects/${pid}/review`);
  const list = page.getByRole("list", { name: "Review issues", exact: true });
  await expect(list.getByRole("listitem")).toHaveCount(2);
  await expect(
    list.getByText("Exact source evidence", { exact: true }),
  ).not.toBeVisible();
  await page.getByLabel("Search issues").fill("missing");
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await list.getByText("Evidence and details", { exact: true }).click();
  await expect(
    list.getByText("Exact source evidence", { exact: true }),
  ).toBeVisible();
  await expect(list.getByText("Pending", { exact: true })).toBeVisible();
});

test("progress keeps runtime and matching live progress visible while folding advanced sections", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "error" },
    [`/projects/${pid}/stats`]: {
      usage: { totals: { total_tokens: 100 } },
      timing: { total_seconds: 123.45 },
    },
  });
  await page.goto(`/projects/${pid}`);
  await expect(page.getByText("Advanced actions", { exact: true })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Preparation", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Resume task", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start translation", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Reassemble in the default format" }),
  ).toHaveCount(0);
  await expect(page.getByText("2m 3s", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Translate chapters in batches", { exact: true }),
  ).not.toBeVisible();
  await page.getByText("Workflow details", { exact: true }).click();
  await expect(
    page.getByText("Translate chapters in batches", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Update report", exact: true }),
  ).not.toBeVisible();
  await page.getByText("Project report", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Update report", exact: true }),
  ).toBeVisible();
});

test("export history shows only five files with its retention notice", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/exports`]: Array.from({ length: 7 }, (_, i) => ({
      id: 7 - i,
      project_id: pid,
      format: "txt",
      status: "done",
      size: 100,
      path: `book-${7 - i}.txt`,
      created_at: "2026-09-17T12:00:00Z",
      options: {},
    })),
  });
  await page.goto(`/projects/${pid}/export`);
  await expect(
    page.getByText(/The server keeps the latest 5 completed exports/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Download", exact: true }),
  ).toHaveCount(5);
  await expect(page.locator("tbody tr")).toHaveCount(5);
});

test("folded export options preserve edits and reveal controls after a validation failure", async ({
  page,
}) => {
  await fakeApi(page);
  let submitted: Record<string, unknown> = {};
  await page.route(`**/api/projects/${pid}/exports`, async (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: [] });
    submitted = route.request().postDataJSON();
    return route.fulfill({
      status: 422,
      json: { detail: "Invalid PDF engine" },
    });
  });
  await page.goto(`/projects/${pid}/export`);
  await page.getByRole("button", { name: "PDF", exact: true }).click();
  await page.getByRole("radio", { name: "Bilingual", exact: true }).check();
  const advanced = page
    .locator("summary")
    .filter({ hasText: "Layout and advanced options" });
  await expect(page.getByLabel("PDF export engine")).not.toBeVisible();
  await advanced.click();
  await page.getByLabel("Bilingual order").selectOption("source_first");
  await page.getByLabel("PDF export engine").selectOption("fpdf2");
  await page.getByLabel("Normalize punctuation on export").uncheck();
  await page.getByLabel("Include an “About this translation” page").uncheck();
  await advanced.click();
  await expect(advanced).toContainText("Source first");
  await page
    .getByRole("button", { name: "Generate export", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Invalid PDF engine");
  await expect(page.getByLabel("PDF export engine")).toBeVisible();
  await expect(page.getByLabel("PDF export engine")).toHaveValue("fpdf2");
  expect(submitted).toMatchObject({
    format: "pdf",
    bilingual: true,
    order: "source_first",
    about_page: false,
    pdf_engine: "fpdf2",
    punctuation_normalize: false,
  });
});
