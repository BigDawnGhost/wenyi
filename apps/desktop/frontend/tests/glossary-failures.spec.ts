import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../../../packages/ui/sandbox/fixtures";
import { registerDesktopSetup } from "./shared-setup";

const apiOrigin = "http://127.0.0.1:19481";
registerDesktopSetup(apiOrigin);

const terms = Array.from({ length: 3 }, (_, index) => ({
  source: `Term ${index}`,
  target: `Translation ${index}`,
  type: "term",
  reading: "",
  note: "",
  gender: "",
  aliases: [],
}));

test("Desktop glossary keeps the failed term of a batch delete selected", async ({ page }) => {
  await page.addInitScript(() => {
    window.confirm = () => true;
  });
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: terms }, apiOrigin);
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
  await page.getByRole("button", { name: "Delete selected", exact: true }).click();
  await expect(page.getByText(/Deleted 2, failed 1/)).toBeVisible();
  await expect(page.getByRole("row", { name: /Term 1/ }).getByRole("checkbox"))
    .toBeChecked();
});
