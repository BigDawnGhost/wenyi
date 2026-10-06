import { test, expect, type Route } from "@playwright/test";
import { configuration, fakeApi, pid, project } from "./fixtures";

const preview = {
  title: "Source preview",
  fmt: "docx",
  chapter_count: 1,
  total_word_count: 12,
  chapters: [{ index: 0, title: "One", word_count: 12 }],
};

async function metadata(route: Route) {
  const form = await new Response(
    new Uint8Array(route.request().postDataBuffer()!),
    {
      headers: { "content-type": route.request().headers()["content-type"] },
    },
  ).formData();
  return JSON.parse(String(form.get("project")));
}

test("creation enters the overview while preparation continues after reload", async ({
  page,
}) => {
  await fakeApi(page, { [`/projects/${pid}/preview`]: preview });
  let status = "preparing";
  let selectedPreparation = false;
  await page.route(`**/api/projects/${pid}`, (r) =>
    r.fulfill({
      json: {
        ...project,
        status,
        fmt: "docx",
        source_meta: { original_filename: "book.docx" },
      },
    }),
  );
  await page.route("**/api/projects", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    const submitted = await metadata(r);
    selectedPreparation = submitted.prepare;
    expect(submitted.translation_mode).toBe("standard");
    expect(submitted).not.toHaveProperty("precision_concurrency");
    await r.fulfill({ json: { ...project, status, fmt: "docx" } });
  });
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("New book");
  await page.getByLabel("Upload source", { exact: true }).setInputFiles({
    name: "book.docx",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("fixture"),
  });
  await page
    .getByRole("checkbox", { name: "Prepare before translating" })
    .check();
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  await expect(page).toHaveURL(`/projects/${pid}`);
  await expect(page.getByRole("heading", { name: "Translation overview", exact: true, level: 2 })).toBeVisible();
  expect(selectedPreparation).toBe(true);
  const start = page.getByRole("button", {
    name: "Start translation",
    exact: true,
  });
  await expect(start).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("Preparing", { exact: true })).toBeVisible();
  await expect(start).toHaveCount(0);
  status = "prepared";
  await expect(start).toBeEnabled();
});

test("creation rejects empty files and recovers from an upload failure without losing the selection", async ({
  page,
}) => {
  await fakeApi(page);
  let requests = 0;
  await page.route("**/api/projects", (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    requests += 1;
    if (requests === 2) return r.fulfill({ json: { ...project, status: "parsing" } });
    return r.fulfill({ status: 503, json: { detail: "Upload unavailable" } });
  });
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("New book");
  const create = page.getByRole("button", {
    name: "Create project",
    exact: true,
  });
  await expect(create).toBeDisabled();
  const file = page.getByLabel("Upload source", { exact: true });
  await file.setInputFiles({
    name: "empty.docx",
    mimeType: "application/octet-stream",
    buffer: Buffer.alloc(0),
  });
  await expect(
    page.getByText("The source file is empty. Choose a file with content."),
  ).toBeVisible();
  await expect(create).toBeDisabled();
  expect(requests).toBe(0);
  await file.setInputFiles({
    name: "book.docx",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("fixture"),
  });
  await create.click();
  await expect(page.getByText("503: Upload unavailable")).toBeVisible();
  await expect(page).toHaveURL("/projects/new");
  await expect(page.getByLabel("Project name", { exact: true })).toHaveValue("New book");
  await expect(page.getByText("book.docx", { exact: true })).toBeVisible();
  await expect(create).toBeEnabled();
  expect(requests).toBe(1);
  await expect(page.getByRole("dialog", { name: "Create project", exact: true })).toBeVisible();
  await create.click();
  await expect(page).toHaveURL(`/projects/${pid}`);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(requests).toBe(2);
});

test("PDF parser is selected before creation and subtitles omit book preparation", async ({
  page,
}) => {
  await fakeApi(page);
  let submitted: Record<string, unknown> | undefined;
  await page.route("**/api/projects", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    submitted = await metadata(r);
    await r.fulfill({ status: 503, json: { detail: "Upload unavailable" } });
  });
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("New book");
  const file = page.getByLabel("Upload source", { exact: true });
  await file.setInputFiles({
    name: "book.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("fixture"),
  });
  await page.getByLabel("PDF parser", { exact: true }).click();
  await page.getByRole("option", { name: "BabelDOC", exact: true }).click();
  await page
    .getByRole("checkbox", { name: "Prepare before translating" })
    .check();
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  await expect(page.getByText("503: Upload unavailable")).toBeVisible();
  expect(submitted?.pdf_backend).toBe("babeldoc");
  expect(submitted?.prepare).toBe(true);
  expect(submitted?.translation_mode).toBe("standard");
  expect(submitted).not.toHaveProperty("precision_concurrency");
  await page.getByRole("radio", { name: "Three drafts + synthesis", exact: true }).check();
  await file.setInputFiles({
    name: "movie.srt",
    mimeType: "text/plain",
    buffer: Buffer.from("fixture"),
  });
  await expect(page.getByLabel("PDF parser", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("radio")).toHaveCount(0);
  await expect(page.getByLabel("Parallel initial drafts (1–3)", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: "Prepare before translating" }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  await expect(page.getByText("503: Upload unavailable")).toBeVisible();
  expect(submitted?.pdf_backend).toBeNull();
  expect(submitted?.prepare).toBe(false);
  expect(submitted?.translation_mode).toBe("standard");
  expect(submitted).not.toHaveProperty("precision_concurrency");
});

test("precision creation saves multipart options and enters the overview after reload", async ({ page }) => {
  await fakeApi(page, {
    [`/projects/${pid}/preview`]: preview,
    [`/projects/${pid}/config`]: {
      ...configuration,
      effective: {
        ...configuration.effective,
        pipeline: { ...configuration.effective.pipeline, translation_mode: "best_of_three", polish: true },
      },
    },
  });
  let submitted: Record<string, unknown> | undefined;
  await page.route("**/api/projects", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted = await metadata(route);
    await route.fulfill({ json: { ...project, fmt: "docx" } });
  });
  await page.goto("/projects/new");
  const standard = page.getByRole("radio", { name: "Standard", exact: true });
  const mode = page.getByRole("radio", { name: "Three drafts + synthesis", exact: true });
  await expect(standard).toBeChecked();
  await standard.focus();
  await page.keyboard.press("ArrowRight");
  await expect(mode).toBeChecked();
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
  await page.getByLabel("Project name", { exact: true }).fill("Precision book");
  await page.getByLabel("Upload source", { exact: true }).setInputFiles({
    name: "book.docx", mimeType: "application/octet-stream", buffer: Buffer.from("fixture"),
  });
  const create = page.getByRole("button", { name: "Create project", exact: true });
  await expect(create).toBeEnabled();
  await create.click();
  await expect(page).toHaveURL(`/projects/${pid}`);
  await expect(page.getByRole("heading", { name: "Translation overview", exact: true, level: 2 })).toBeVisible();
  expect(submitted?.translation_mode).toBe("best_of_three");
  expect(submitted).not.toHaveProperty("precision_concurrency");
  await page.reload();
  await expect(page).toHaveURL(`/projects/${pid}`);
  await expect(page.getByRole("heading", { name: "Translation overview", exact: true, level: 2 })).toBeVisible();
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
});

test("creation with a failed preparation queue enters the overview and retries after reload", async ({
  page,
}) => {
  await fakeApi(page, { [`/projects/${pid}/preview`]: preview });
  let status = "error";
  let resumed = false;
  await page.route(`**/api/projects/${pid}`, (r) =>
    r.fulfill({
      json: {
        ...project,
        status,
        error: status === "error" ? "Task queue is unavailable" : null,
        source_meta: { original_filename: "book.docx" },
      },
    }),
  );
  await page.route(`**/api/projects/${pid}/resume`, (r) => {
    status = "preparing";
    resumed = true;
    return r.fulfill({
      json: { job_id: "prepare-2", kind: "prepare", project_id: pid },
    });
  });
  await page.route("**/api/projects", (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    return r.fulfill({
      json: { ...project, status, error: "Task queue is unavailable" },
    });
  });
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("Queue failure");
  await page.getByLabel("Upload source", { exact: true }).setInputFiles({
    name: "book.docx",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("fixture"),
  });
  await page.getByRole("button", { name: "Create project", exact: true }).click();
  await expect(page).toHaveURL(`/projects/${pid}`);
  await expect(page.getByText("Task queue is unavailable", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Task queue is unavailable", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create project", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Resume task", exact: true }).click();
  await expect(
    page.getByText("Task queue is unavailable", { exact: true }),
  ).toHaveCount(0);
  expect(resumed).toBe(true);
  status = "prepared";
  await expect(
    page.getByRole("button", { name: "Start translation", exact: true }),
  ).toBeEnabled();
});

test("Chinese creation fits mobile and enters the overview before parsing finishes", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await page.setViewportSize({ width: 390, height: 844 });
  await fakeApi(page);
  let ready = false;
  await page.route("**/api/projects", (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    return r.fulfill({
      json: {
        ...project,
        status: "parsing",
        fmt: "docx",
        source_meta: { original_filename: "原文.docx" },
      },
    });
  });
  await page.route(`**/api/projects/${pid}`, (r) => {
    return r.fulfill({
      json: { ...project, status: ready ? "uploaded" : "parsing", fmt: "docx" },
    });
  });
  await page.route(`**/api/projects/${pid}/preview`, (r) => {
    return r.fulfill({
      status: 409,
      json: { detail: "Source preview is not ready" },
    });
  });
  await page.goto("/projects/new");
  await page.getByLabel("项目名称", { exact: true }).fill("新书翻译");
  await page.getByRole("radio", { name: "三译合润", exact: true }).check();
  await expect(page.getByRole("spinbutton")).toHaveCount(0);
  await expect(page.getByText("精翻自动为本项目启用润色，不受全局设置影响。", { exact: false })).toBeVisible();
  await page.getByLabel("上传原文", { exact: true }).setInputFiles({
    name: "原文.docx",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("fixture"),
  });
  await expect(
    page.getByRole("checkbox", { name: "创建后执行译前准备" }),
  ).not.toBeChecked();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(page).toHaveURL(`/projects/${pid}`);
  await expect(page.getByText("解析中", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "开始翻译", exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("解析中", { exact: true })).toBeVisible();
  ready = true;
  await expect(
    page.getByRole("button", { name: "开始翻译", exact: true }),
  ).toBeEnabled();
  await expect(page.getByText("Source preview", { exact: true })).toHaveCount(0);
});
