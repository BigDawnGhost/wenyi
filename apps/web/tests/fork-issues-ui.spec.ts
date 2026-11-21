import { expect, test } from "@playwright/test";
import {
  fakeApi,
  pid,
  project,
  termPageFixture,
} from "../../../packages/ui/sandbox/fixtures";

test("glossary surfaces frequency-sorted conflicts above the table and marks entries context-dependent", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "en"));
  let resolved = false;
  let capturedDecision: Record<string, unknown> | null = null;
  const terms = [
    {
      source: "洗白",
      target: "go legit",
      reading: "",
      type: "term",
      gender: "",
      aliases: [],
      first_chapter: 0,
      note: "",
      status: "conflict",
    },
  ];
  await fakeApi(page, {});
  await page.route(`**/api/projects/${pid}/glossary/terms**`, (route) => {
    const current = resolved
      ? [
          {
            ...terms[0],
            target: "go legit | whitewash | launder",
            status: "context_dependent",
          },
        ]
      : terms;
    return route.fulfill({ json: termPageFixture(current, route.request().url()) });
  });
  await page.route(`**/api/projects/${pid}/glossary/conflicts`, (route) =>
    route.fulfill({
      json: resolved
        ? []
        : [
            {
              id: 7,
              source: "洗白",
              existing_target: "go legit",
              proposed_target: "whitewash",
              chapter: 2,
              conflict_count: 3,
            },
          ],
    }),
  );
  await page.route(`**/api/projects/${pid}/glossary/conflicts/7/resolve`, (route) => {
    capturedDecision = route.request().postDataJSON();
    resolved = true;
    return route.fulfill({ json: { message: "resolved" } });
  });

  await page.goto(`/projects/${pid}/glossary`);
  await expect(page.getByText("×3")).toBeVisible();
  const markBtn = page.getByRole("button", { name: "Mark context-dependent", exact: true });
  await expect(markBtn).toBeVisible();
  await markBtn.click();

  await expect.poll(() => capturedDecision).toEqual({ decision: "context_dependent" });
  await expect(page.getByText("Context-dependent")).toBeVisible();
  await expect(page.getByText("go legit | whitewash | launder")).toBeVisible();
});

test("progress page shows scheduled automatic retry time and Resume now after provider outage", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "en"));
  let resumed = false;
  await fakeApi(page, {
    [`/projects/${pid}`]: {
      ...project,
      status: "paused",
      error: "Model request failed: status=500",
    },
    [`/projects/${pid}/workflow`]: {
      source: "snapshot",
      kind: "translation",
      status: "paused",
      run_id: "run-outage",
      review_id: null,
      stages: [],
      progress: {
        run_id: "run-outage",
        project_id: pid,
        kind: "translation",
        done: 12,
        total: 40,
        label: "progress.stage.outagePaused",
        payload: {
          next_retry_at: "2026-11-21T12:30:00+00:00",
          delay_seconds: 60,
          attempt: 1,
          reason: "Model request failed: status=500",
        },
        updated_at: "2026-11-21T12:29:00+00:00",
        elapsed_seconds: 120,
      },
    },
  });
  await page.route(`**/api/projects/${pid}/resume`, (route) => {
    resumed = true;
    return route.fulfill({
      json: { job_id: "run-resumed", project_id: pid, kind: "translation" },
    });
  });

  await page.goto(`/projects/${pid}`);
  await expect(page.getByText("Paused after provider outage")).toBeVisible();
  await expect(page.getByText(/Automatic retry scheduled at /)).toBeVisible();
  const resumeNow = page.getByRole("button", { name: "Resume now", exact: true });
  await expect(resumeNow).toBeVisible();
  await resumeNow.click();
  await expect.poll(() => resumed).toBe(true);
});
