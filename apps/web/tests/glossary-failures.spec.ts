import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../../packages/ui/sandbox/fixtures";
import { scenarios } from "../../../packages/ui/sandbox/scenarios";
import { dragSelection, textPoint } from "../../../packages/ui/tests/e2e/text-selection";

const terms = Array.from({ length: 3 }, (_, index) => ({
  source: `Term ${index}`,
  target: `Translation ${index}`,
  type: "term",
  reading: "",
  note: "",
  gender: "",
  aliases: [],
}));

test("mouse selection from a conflict term to the row end excludes actions", async ({ page }) => {
  await fakeApi(page, scenarios.glossaryConflicts.overrides);
  await page.goto(`/projects/${pid}/glossary`);
  const source = page.getByText("霧の谷", { exact: true });
  const keep = page.getByRole("button", { name: "Keep current", exact: true });
  const accept = page.getByRole("button", { name: "Accept suggestion", exact: true });
  await expect(keep).toBeEnabled();
  await accept.scrollIntoViewIfNeeded();
  const end = (await accept.boundingBox())!;
  const text = await dragSelection(page, await textPoint(source, "start"), {
    x: end.x + end.width - 2, y: end.y + end.height / 2,
  });
  expect(text).toContain("霧の谷");
  expect(text).toContain("Mist Valley");
  expect(text).toContain("Valley of Mist");
  expect(text).not.toMatch(/Current:|AI suggestion:|Keep current|Accept suggestion/);
  await expect(accept).toBeVisible();
});

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
