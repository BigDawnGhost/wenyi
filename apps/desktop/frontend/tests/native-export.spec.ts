import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

const origin = "http://127.0.0.1:19481";

test("HTML native save preserves the options and reports the archive path", async ({ page }) => {
  await page.addInitScript(() => {
    window.__WENYI_DESKTOP__ = { apiBase: "http://127.0.0.1:19481", token: "fixture-token" };
    window.__TAURI_INTERNALS__ = {
      invoke: (_command, args) => {
        Object.assign(window, { savedOptions: args.options });
        return Promise.resolve({ path: "/fixture/book.html.zip" } as never);
      },
    };
  });
  await fakeApi(page, {}, origin);
  let browserJobs = 0;
  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/exports")) browserJobs++;
  });
  await page.goto(`/projects/${pid}/export`);
  await page.getByRole("button", { name: "HTML", exact: true }).click();
  await page.getByRole("button", { name: "Generate export", exact: true }).click();
  await expect(page.getByText("Saved to /fixture/book.html.zip")).toBeVisible();
  expect(await page.evaluate(() =>
    (window as unknown as { savedOptions: { format: string } }).savedOptions.format,
  )).toBe("html");
  expect(browserJobs).toBe(0);
});

test("Desktop cancellation creates no browser job; history uses native Save as", async ({ page }) => {
  await page.addInitScript(() => {
    window.__WENYI_DESKTOP__ = { apiBase: "http://127.0.0.1:19481", token: "fixture-token" };
    Object.assign(window, { saveCalls: [] });
    window.__TAURI_INTERNALS__ = {
      invoke: (command, args) => {
        (window as unknown as { saveCalls: unknown[] }).saveCalls.push({ command, args });
        return Promise.resolve(null as never);
      },
    };
  });
  await fakeApi(page, {
    [`/projects/${pid}/exports`]: [
      { id: 7, format: "docx", status: "done", size: 123, options: {}, created_at: null },
    ],
  }, origin);
  let jobs = 0;
  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/exports")) jobs++;
  });
  await page.goto(`/projects/${pid}/export`);
  await page.getByRole("button", { name: "Generate export", exact: true }).click();
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as { saveCalls: unknown[] }).saveCalls.length)).toBe(1);
  await expect(page.getByText("Export task submitted", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Save as", exact: true }).click();
  const calls = await page.evaluate(() =>
    (window as unknown as { saveCalls: { command: string; args: Record<string, unknown> }[] }).saveCalls);
  expect(calls[0].command).toBe("native_export_save");
  expect(calls[0].args.projectId).toBe(pid);
  expect(Object.keys(calls[0].args).sort()).toEqual(["options", "projectId"]);
  expect(calls[1]).toEqual({
    command: "native_export_save", args: { projectId: pid, options: {}, exportId: 7 },
  });
  expect(jobs).toBe(0);
});

test("Desktop saving completes and reports its path after leaving the export page", async ({ page }) => {
  await page.addInitScript(() => {
    window.__WENYI_DESKTOP__ = { apiBase: "http://127.0.0.1:19481", token: "fixture-token" };
    window.__TAURI_INTERNALS__ = {
      invoke: () => new Promise(resolve => Object.assign(window, { finishSave: resolve })),
    };
  });
  await fakeApi(page, {}, origin);
  await page.goto(`/projects/${pid}/export`);
  await page.getByRole("button", { name: "Generate export", exact: true }).click();
  await expect.poll(() => page.evaluate(() => "finishSave" in window)).toBe(true);
  // Client-side navigation does not destroy the IPC promise or Rust task.
  await page.getByRole("link", { name: "Translation overview", exact: true }).click();
  await page.evaluate(() => {
    (window as unknown as { finishSave: (value: unknown) => void })
      .finishSave({ path: "/fixture/destination/book.zh.docx" });
  });
  await expect(page.getByText("Saved to /fixture/destination/book.zh.docx")).toBeVisible();
});
