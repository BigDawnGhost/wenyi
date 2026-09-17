import { test, expect } from "@playwright/test";
import { fakeApi, pid, chapter } from "./fixtures";

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
