import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../../packages/ui/sandbox/fixtures";

const terms = Array.from({ length: 3 }, (_, index) => ({
  source: `Term ${index}`,
  target: `Translation ${index}`,
  type: "term",
  reading: "",
  note: "",
  gender: "",
  aliases: [],
}));

test("deleting the selection reports which terms survived", async ({ page }) => {
  await page.addInitScript(() => {
    window.confirm = () => true;
  });
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: terms });
  await page.route(`**/projects/${pid}/glossary/terms{,/*}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    const source = decodeURIComponent(route.request().url().split("/").pop() || "");
    return source === "Term 1"
      ? route.fulfill({
        status: 409,
        json: { code: "project_busy", message: "Busy.", detail: "Busy." },
      })
      : route.fulfill({ json: { message: "deleted" } });
  });
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("checkbox", { name: "Select all terms on this page" }).check();
  await expect(page.getByText("Selected: 3", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Delete selected", exact: true }).click();
  // Two terms were deleted and one was refused, so the toast cannot claim
  // success and the refused row must stay selected for an exact retry.
  await expect(page.getByText(/Deleted 2, failed 1/)).toBeVisible();
  await expect(page.getByText("Selected: 1", { exact: true })).toBeVisible();
  const surviving = page.getByRole("row", { name: /Term 1/ });
  await expect(surviving.getByRole("checkbox")).toBeChecked();
  // The page-level box cannot stay checked once only one row remains selected.
  await expect(
    page.getByRole("checkbox", { name: "Select all terms on this page" }),
  ).not.toBeChecked();
});

test("an import whose read fails reports it instead of staying on upload", async ({ page }) => {
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: terms });
  // The browser cannot produce an unreadable File, so the read is failed the
  // way a revoked or detached file fails it: no load, only an error event.
  await page.addInitScript(() => {
    FileReader.prototype.readAsText = function (this: FileReader) {
      setTimeout(() => this.onerror?.(new ProgressEvent("error") as ProgressEvent<FileReader>), 0);
    };
  });
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Import", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Import terms", exact: true });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: "terms.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify([{ source: "A", target: "B" }])),
  });
  await expect(page.getByText("Could not read the selected file", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Choose file", exact: true })).toBeVisible();
});

test("an aborted read reports the same failure as a refused one", async ({ page }) => {
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: terms });
  await page.addInitScript(() => {
    FileReader.prototype.readAsText = function (this: FileReader) {
      setTimeout(() => this.onabort?.(new ProgressEvent("abort") as ProgressEvent<FileReader>), 0);
    };
  });
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Import", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Import terms", exact: true });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: "terms.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("source,target\nA,B\n"),
  });
  await expect(page.getByText("Could not read the selected file", { exact: true })).toBeVisible();
});
