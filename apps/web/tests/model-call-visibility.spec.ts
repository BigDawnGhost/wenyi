import { expect, test, type Page, type Request } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

// The provider call is the part of a run the reader cannot see or predict.
// These cover the two things they can be told: how long a call took, and how
// much of the answer was spent reasoning rather than answering.

function event(
  id: number,
  type: string,
  payload: Record<string, unknown>,
  created_at: string,
) {
  return { id, type, payload, created_at };
}

const modelEvents = [
  event(
    10,
    "llm_request_completed",
    { operation: "translation.body", attempt: 1, attempt_seconds: 560.4 },
    "2026-09-23T02:00:00Z",
  ),
  event(
    9,
    "llm_retry_wait",
    {
      operation: "translation.body",
      reason: "empty_response",
      failed_attempt: 1,
      next_attempt: 2,
      max_attempts: 5,
      wait_seconds: 0.7,
    },
    "2026-09-23T01:59:00Z",
  ),
  event(
    8,
    "llm_request_completed",
    { operation: "glossary.extract", attempt: 1, attempt_seconds: 12.3 },
    "2026-09-23T01:58:00Z",
  ),
];

test("a long provider call is called out on its own row", async ({ page }) => {
  await fakeApi(page, { [`/projects/${pid}/events`]: modelEvents });
  await page.goto(`/projects/${pid}/events`);

  await expect(page.getByText("Slow model call, took 9m 20s")).toBeVisible();
  await expect(page.getByText("took 12.3 s").first()).toBeVisible();
});

test("a retry names the reason instead of showing a raw code", async ({ page }) => {
  await fakeApi(page, { [`/projects/${pid}/events`]: modelEvents });
  await page.goto(`/projects/${pid}/events`);

  await expect(
    page.getByText(
      "Attempt 1/5 on Translate chapters in batches did not work (the reply was empty); retrying in 0.7 s",
    ),
  ).toBeVisible();
});

test("a provider status code is reported as one, not as a bare number", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/events`]: [
      {
        id: 4,
        type: "llm_retry_wait",
        payload: {
          operation: "translation.body",
          reason: "http_429",
          failed_attempt: 2,
          max_attempts: 5,
          wait_seconds: 12,
        },
        created_at: "2026-09-23T02:05:00Z",
      },
      {
        id: 3,
        type: "llm_retry_wait",
        payload: {
          operation: "translation.body",
          reason: "http_503",
          failed_attempt: 1,
          max_attempts: 5,
          wait_seconds: 1,
        },
        created_at: "2026-09-23T02:04:00Z",
      },
    ],
  });
  await page.goto(`/projects/${pid}/events`);

  await expect(
    page.getByText(/the provider rate limit was reached/).first(),
  ).toBeVisible();
  await expect(
    page.getByText(/the provider returned HTTP 503/),
  ).toBeVisible();
});

test("reasoning tokens are separated from the answer they are billed with", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      usage: {
        schema_version: 2,
        totals: {
          calls: 2,
          prompt_tokens: 1000,
          completion_tokens: 900,
          total_tokens: 1900,
          cache_hit_tokens: 0,
          cache_miss_tokens: 1000,
          reasoning_tokens: 700,
        },
        by_stage: {
          "translation.body": {
            calls: 2,
            prompt_tokens: 1000,
            completion_tokens: 900,
            total_tokens: 1900,
            cache_hit_tokens: 0,
            cache_miss_tokens: 1000,
            reasoning_tokens: 700,
          },
        },
        by_model: {},
        by_provider: {},
        labels: {},
      },
      timing: { runs: [], total_seconds: 0 },
    },
  });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", {
    name: "Total usage & run time",
  });

  // Reasoning is billed inside completion_tokens, so the bar has to show the
  // 200 answered tokens separately from the 700 spent thinking.
  await expect(accounting.locator("dl")).toContainText("Reasoning tokens");
  await expect(accounting.locator("dl")).toContainText("700");
  const stage = accounting.getByRole("list", { name: "By stage", exact: true });
  await expect(stage).toContainText("Reasoning: 700");
  await expect(stage).toContainText("Answered output: 200");
});

test("a ledger without reasoning accounting does not claim a measured zero", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      usage: {
        schema_version: 2,
        totals: {
          calls: 2,
          prompt_tokens: 1000,
          completion_tokens: 900,
          total_tokens: 1900,
          cache_hit_tokens: 0,
          cache_miss_tokens: 1000,
        },
        by_stage: {},
        by_model: {},
        by_provider: {},
        labels: {},
      },
      timing: { runs: [], total_seconds: 0 },
    },
  });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", {
    name: "Total usage & run time",
  });
  await expect(accounting.locator("dl")).not.toContainText("Reasoning tokens");
  await expect(accounting.locator("dl")).toContainText("1,900");
});

function countApiRequests(page: Page) {
  const counts = new Map<string, number>();
  const listener = (request: Request) => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/")) return;
    counts.set(url.pathname, (counts.get(url.pathname) ?? 0) + 1);
  };
  page.on("request", listener);
  return {
    counts,
    stop: () => page.off("request", listener),
  };
}

test("a finished book stops asking for progress", async ({ page }) => {
  await page.clock.install();
  await fakeApi(page);
  const tracker = countApiRequests(page);
  await page.goto(`/projects/${pid}`);
  await expect(
    page.getByRole("heading", { name: "Test Book" }),
  ).toBeVisible();
  // Opening the progress socket reconciles once against it; after that a book
  // with status "done" has nothing left to watch for. Left polling, this page
  // fired roughly sixty requests a minute forever.
  await page.clock.runFor(2_000);
  const settled = new Map(tracker.counts);
  expect(settled.get(`/api/projects/${pid}`)).toBeGreaterThan(0);

  await page.clock.runFor(120_000);
  for (const path of [
    `/api/projects/${pid}`,
    `/api/projects/${pid}/chapters`,
    `/api/projects/${pid}/workflow`,
  ]) {
    expect(tracker.counts.get(path), path).toBe(settled.get(path));
  }
  tracker.stop();
});

test("a running book keeps refreshing while it works", async ({ page }) => {
  await page.clock.install();
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ json: { ...project, status: "translating" } }),
  );
  const tracker = countApiRequests(page);
  await page.goto(`/projects/${pid}`);
  await expect(
    page.getByRole("heading", { name: "Test Book" }),
  ).toBeVisible();
  const before = tracker.counts.get(`/api/projects/${pid}`) ?? 0;
  await page.clock.runFor(10_000);
  expect(tracker.counts.get(`/api/projects/${pid}`)).toBeGreaterThan(before);
  tracker.stop();
});