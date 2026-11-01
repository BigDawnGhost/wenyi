import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../../../packages/ui/tests/e2e/fixtures";

const origin = "http://127.0.0.1:19481";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.__WENYI_DESKTOP__ = {
      apiBase: "http://127.0.0.1:19481", token: "fixture-token",
    };
    Object.assign(window, { glossarySaveCalls: [] });
    window.__TAURI_INTERNALS__ = {
      invoke: (command, args) => {
        if (command !== "native_glossary_save") return Promise.resolve(null as never);
        (window as unknown as { glossarySaveCalls: unknown[] })
          .glossarySaveCalls.push({ command, args });
        return new Promise((resolve, reject) =>
          Object.assign(window, { finishGlossarySave: resolve, failGlossarySave: reject }));
      },
    };
  });
  await fakeApi(page, {}, origin);
});

for (const format of ["csv", "json", "apkg"] as const) {
  test(`glossary ${format} uses native saving and reports the destination`, async ({ page }) => {
    const browserDownloads: string[] = [];
    page.on("download", download => browserDownloads.push(download.suggestedFilename()));
    await page.goto(`/projects/${pid}/glossary?type=person`);
    await page.getByRole("button", { name: "Export", exact: true }).click();
    await expect(page.getByText(/Exports all terms, regardless of filters or selection/)).toHaveCount(0);
    const label = format === "apkg" ? "Anki deck (.apkg)" : `${format.toUpperCase()} file`;
    await page.getByRole("button", { name: label, exact: true }).click();
    await expect.poll(() => page.evaluate(() =>
      (window as unknown as { glossarySaveCalls: unknown[] }).glossarySaveCalls,
    )).toEqual([{
      command: "native_glossary_save", args: { projectId: pid, format },
    }]);
    const button = page.getByRole("button", { name: "Export", exact: true });
    await expect(button).toBeDisabled();
    await page.evaluate(format => {
      (window as unknown as { finishGlossarySave: (value: unknown) => void })
        .finishGlossarySave({ path: `/fixture/术语.${format}` });
    }, format);
    await expect(page.getByText(`Saved to /fixture/术语.${format}`, { exact: true })).toBeVisible();
    await expect(button).toBeEnabled();
    expect(browserDownloads).toEqual([]);
  });
}

for (const structured of [false, true]) {
  test(`canceling a glossary save allows retry; ${structured ? "structured" : "legacy"} failures are visible`, async ({ page }) => {
    await page.goto(`/projects/${pid}/glossary`);
    const button = page.getByRole("button", { name: "Export", exact: true });
    await button.click();
    await page.getByRole("button", { name: "Anki deck (.apkg)", exact: true }).click();
    await expect(button).toBeDisabled();
    await page.evaluate(() => {
      (window as unknown as { finishGlossarySave: (value: unknown) => void }).finishGlossarySave(null);
    });
    await expect(button).toBeEnabled();
    await expect(page.getByText(/^Saved to /)).toHaveCount(0);
    await button.click();
    await page.getByRole("button", { name: "Anki deck (.apkg)", exact: true }).click();
    await expect(button).toBeDisabled();
    await page.evaluate(structured => {
      const message = "The destination is read-only.";
      (window as unknown as { failGlossarySave: (reason: unknown) => void })
        .failGlossarySave(structured ? { code: "export_write_failed", message } : message);
    }, structured);
    await expect(page.getByText("The destination is read-only.", { exact: true })).toBeVisible();
    await expect(button).toBeEnabled();
  });
}

test("glossary export without native IPC reports an error instead of a browser download", async ({ page }) => {
  await page.goto(`/projects/${pid}/glossary`);
  await page.evaluate(() => { delete window.__TAURI_INTERNALS__; });
  await page.getByRole("button", { name: "Export", exact: true }).click();
  await page.getByRole("button", { name: "CSV file", exact: true }).click();
  await expect(page.getByText("Native saving is unavailable.", { exact: true })).toBeVisible();
});

test("glossary saving reports completion after navigating to another page", async ({ page }) => {
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Export", exact: true }).click();
  await page.getByRole("button", { name: "JSON file", exact: true }).click();
  await expect(page.getByRole("button", { name: "Export", exact: true })).toBeDisabled();
  await page.getByRole("link", { name: "Translation overview", exact: true }).click();
  await page.evaluate(() => {
    (window as unknown as { finishGlossarySave: (value: unknown) => void })
      .finishGlossarySave({ path: "/fixture/术语.json" });
  });
  await expect(page.getByText("Saved to /fixture/术语.json", { exact: true })).toBeVisible();
});
