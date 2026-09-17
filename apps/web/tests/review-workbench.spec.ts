import { expect, test } from "@playwright/test";
import { fakeApi, pid, project, workflow } from "./fixtures";

const started = "2026-09-17T09:00:00Z";
const run = {
  id: "review-current",
  review_id: "review-current",
  status: "running",
  created_at: started,
  summary: { issue_count: 0, change_count: 0 },
  issues: [],
  changes: [],
  autofix: {},
  result: { started_at: started },
  items: [],
};
const location = {
  chapter: 0,
  text_index: 1,
  segment_index: 18,
  chapter_title: "Chapter One",
  source: "Source evidence",
  current_target: "Current saved translation",
};
const item = {
  id: "issue:a",
  kind: "issue",
  type: "missing",
  detail: "A missing phrase",
  suggestion: "Restore the missing phrase",
  status: "pending",
  location,
  issue: { issue_key: "a" },
  evidence: [location],
  changes: [
    {
      suggested_target: "Suggested replacement text",
      review_result: "not_rereported",
    },
  ],
  publications: [],
};

test("running review shows its own progress and a ticking task timer, not zero results", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-09-17T09:00:40Z") });
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "reviewing" },
    [`/projects/${pid}/review/runs`]: [run],
    [`/projects/${pid}/review/runs/${run.id}`]: run,
    [`/projects/${pid}/workflow`]: {
      ...workflow,
      kind: "review",
      status: "running",
      run_id: "task-a",
      review_id: run.id,
      progress: {
        project_id: pid,
        run_id: "task-a",
        kind: "review",
        label: "Whole-book review R1",
        done: 37,
        total: 100,
        elapsed_seconds: 40,
        updated_at: "2026-09-17T09:00:40Z",
      },
    },
  });
  await page.goto(`/projects/${pid}/review`);
  await expect(
    page.getByRole("progressbar", { name: "Current stage progress" }),
  ).toHaveAttribute("aria-valuenow", "37");
  await expect(
    page.getByText("Reviewing the book · round 1", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Results are still being generated.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Run whole-book review", exact: true }),
  ).toHaveCount(0);
  await page.clock.pauseAt(new Date("2026-09-17T09:00:50Z"));
  await expect(page.getByTestId("review-elapsed")).toContainText("50 s");
  await page.clock.runFor(3000);
  await expect(page.getByTestId("review-elapsed")).toContainText("53 s");
  await expect(page.getByText("No issues found", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.getByText(run.id, { exact: true })).not.toBeVisible();
});

test("issue rows separate suggestions from published fixes and link to stable paragraph IDs", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const completed = {
    ...run,
    status: "completed",
    items: [
      item,
      {
        ...item,
        id: "issue:b",
        detail: "A corrected name",
        status: "fixed",
        changes: [],
        publications: [
          {
            status: "applied",
            before: "Old name",
            after: "Intermediate name",
            publication: {
              status: "applied",
              before: "Old name",
              target: "Published name",
            },
          },
        ],
      },
      {
        ...item,
        id: "issue:c",
        detail: "A failed fix",
        status: "failed",
        changes: [],
        publications: [
          {
            status: "not_applied",
            before: "Before",
            after: "Unpublished text",
            publication: { status: "failed", reason: "formal_target_changed" },
          },
        ],
      },
    ],
  };
  await fakeApi(page, {
    [`/projects/${pid}/review/runs`]: [completed],
    [`/projects/${pid}/review/runs/${run.id}`]: completed,
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: "Chapter One",
      segments: [
        {
          index: 18,
          kind: "text",
          source: "Source evidence",
          target: "Current saved translation",
        },
      ],
      review_issues: [],
    },
  });
  await page.goto(`/projects/${pid}/review`);
  const list = page.getByRole("list", { name: "Review issues", exact: true });
  await expect(list.getByRole("listitem")).toHaveCount(3);
  await page.getByLabel("Filter by handling status").selectOption("pending");
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await list.getByText("Evidence and details", { exact: true }).click();
  await expect(
    list.getByText("Suggested replacement text", { exact: true }),
  ).toBeVisible();
  await expect(list.getByText("Written back", { exact: true })).toHaveCount(0);
  await page.screenshot({
    path: testInfo.outputPath("review-workbench.png"),
    fullPage: true,
  });
  await page.getByLabel("Filter by handling status").selectOption("fixed");
  await list.getByText("Evidence and details", { exact: true }).click();
  await expect(list.getByText("Published name", { exact: true })).toBeVisible();
  await expect(
    list.getByText("Intermediate name", { exact: true }),
  ).toHaveCount(0);
  await expect(
    list.getByText("Written-back translation", { exact: true }),
  ).toBeVisible();
  await list
    .getByRole("link", { name: "Open in proofreading", exact: true })
    .click();
  await expect(page).toHaveURL(`/projects/${pid}/proofreading/0?segment=18`);
  await expect(page.locator("#paragraph-18")).toBeFocused();
});

test("history never borrows progress from the current review and empty sections stay hidden", async ({
  page,
}) => {
  const old = {
    ...run,
    id: "review-old",
    review_id: "review-old",
    status: "completed",
    created_at: "2026-09-16T09:00:00Z",
  };
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "reviewing" },
    [`/projects/${pid}/review/runs`]: [run, old],
    [`/projects/${pid}/review/runs/${run.id}`]: run,
    [`/projects/${pid}/review/runs/${old.id}`]: old,
    [`/projects/${pid}/workflow`]: {
      ...workflow,
      kind: "review",
      status: "running",
      run_id: "task-a",
      review_id: run.id,
      progress: {
        project_id: pid,
        run_id: "task-a",
        label: "Whole-book review R2",
        done: 5,
        total: 10,
      },
    },
  });
  await page.goto(`/projects/${pid}/review`);
  await expect(page.getByRole("progressbar")).toBeVisible();
  await page.getByLabel("Review history").selectOption(old.id);
  await expect(
    page.getByText("No issues found", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await expect(
    page.getByText("Autofix & publication records", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Latest result", exact: true })
    .click();
  await expect(page.getByRole("progressbar")).toBeVisible();
});

test("unrelated cached progress is rejected and missing totals do not invent percentages", async ({
  page,
}) => {
  let progress: Record<string, unknown> = {
    project_id: "other-project",
    run_id: "task-a",
    label: "Whole-book review R99",
    done: 50,
    total: 100,
  };
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "reviewing" },
    [`/projects/${pid}/review/runs`]: [run],
    [`/projects/${pid}/review/runs/${run.id}`]: run,
  });
  await page.route(`**/api/projects/${pid}/workflow`, (route) =>
    route.fulfill({
      json: {
        ...workflow,
        kind: "review",
        status: "running",
        run_id: "task-a",
        review_id: run.id,
        progress,
      },
    }),
  );
  await page.goto(`/projects/${pid}/review`);
  await expect(
    page.getByText("Waiting for review progress", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  progress = {
    project_id: pid,
    run_id: "task-a",
    label: "Restoring review checkpoint…",
    done: 0,
    total: 0,
  };
  await expect(
    page.getByText("Restoring saved review progress", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
});

test("paused review freezes elapsed time and retains unfinished findings in Chinese on mobile", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await page.clock.install({ time: new Date("2026-09-17T09:10:00Z") });
  const interrupted = {
    ...run,
    items: [
      {
        ...item,
        detail: "术语译法不一致",
        status: "failed",
        changes: [],
        publications: [
          {
            status: "not_applied",
            before: "旧译文",
            after: "未发布的修订",
            publication: { status: "failed", reason: "formal_target_changed" },
          },
        ],
      },
    ],
  };
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "paused" },
    [`/projects/${pid}/review/runs`]: [interrupted],
    [`/projects/${pid}/review/runs/${run.id}`]: interrupted,
    [`/projects/${pid}/workflow`]: {
      ...workflow,
      kind: "review",
      status: "paused",
      run_id: "task-a",
      review_id: run.id,
      progress: {
        project_id: pid,
        run_id: "task-a",
        label: "Whole-book review R1",
        done: 5,
        total: 10,
        elapsed_seconds: 45,
        updated_at: "2026-09-17T09:00:45Z",
      },
    },
  });
  await page.goto(`/projects/${pid}/review`);
  await expect(page.getByText("已暂停", { exact: true })).toBeVisible();
  await expect(page.getByTestId("review-elapsed")).toContainText("45");
  const before = await page.getByTestId("review-elapsed").textContent();
  await page.clock.runFor(4000);
  await expect(page.getByTestId("review-elapsed")).toHaveText(before!);
  await expect(page.getByText("未发现问题", { exact: true })).toHaveCount(0);
  await page.getByText("证据与详情", { exact: true }).click();
  await expect(
    page.getByText("正式译文已发生变化，因此此次修订未写回。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("未发布的修订", { exact: true })).toBeVisible();
  await expect(
    page
      .getByRole("list", { name: "审校问题", exact: true })
      .getByText("已写回", { exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("review-mobile.png"),
    fullPage: true,
  });
});

test("new review preparation identifies the previous result and ignores stale socket messages", async ({
  page,
}) => {
  const saved = { ...run, status: "completed" };
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: "reviewing" },
    [`/projects/${pid}/review/runs`]: [saved],
    [`/projects/${pid}/review/runs/${run.id}`]: saved,
    [`/projects/${pid}/workflow`]: {
      ...workflow,
      kind: "review",
      status: "running",
      run_id: "task-b",
      review_id: null,
      progress: {
        project_id: pid,
        run_id: "task-b",
        label: "Loading review chapters",
        done: 0,
        total: 0,
        updated_at: "2026-09-17T09:00:00Z",
      },
    },
  });
  await page.routeWebSocket("**/ws/**", (socket) => {
    socket.onMessage(() =>
      socket.send(
        JSON.stringify({
          project_id: pid,
          run_id: "task-a",
          label: "Whole-book review R99",
          done: 99,
          total: 100,
          updated_at: "2026-09-17T10:00:00Z",
        }),
      ),
    );
  });
  await page.goto(`/projects/${pid}/review`);
  await expect(
    page.getByRole("heading", { name: "Loading review chapters", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /Previously saved results/ }),
  ).toBeVisible();
  await expect(
    page.getByText("Results are still being generated.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await expect(
    page.getByText("Reviewing the book · round 99", { exact: true }),
  ).toHaveCount(0);
});
