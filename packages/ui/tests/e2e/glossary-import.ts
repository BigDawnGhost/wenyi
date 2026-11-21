import { expect, test, type Page } from "@playwright/test";
import { fakeApi, pid, termPageFixture } from "../../sandbox/fixtures";

const terms = Array.from({ length: 2183 }, (_, index) => ({
  source: `原文 ${String(index).padStart(4, "0")}`,
  target: `译文 ${index}`,
  reading: "",
  type: "term",
  gender: "",
  aliases: ["别名一", "别名二"],
  note: 'A "quoted", multiline\r\nnote',
}));

export function registerTests(apiOrigin?: string) {
  async function setup(page: Page) {
    const imports: (typeof terms)[] = [];
    const stored: typeof terms = [];
    await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: stored }, apiOrigin);
    await page.route(`**/projects/${pid}/glossary/import`, async (route) => {
      const { terms: entries } = route.request().postDataJSON() as { terms: typeof terms };
      imports.push(entries);
      if (route.request().postDataBuffer()!.length > 5 * 1024 * 1024) {
        return route.fulfill({ status: 413, json: { code: "glossary_import_too_large" } });
      }
      stored.push(...entries);
      return route.fulfill({ json: { imported: entries.length } });
    });
    await page.route(`**/projects/${pid}/glossary/terms/page?**`, (route) =>
      route.fulfill({ json: termPageFixture(stored, route.request().url()) }),
    );
    await page.goto(`/projects/${pid}/glossary`);
    await page.getByRole("button", { name: "Import", exact: true }).click();
    return { dialog: page.getByRole("dialog", { name: "Import terms" }), imports };
  }

  test("a 2183-term CSV export fits in a single request", async ({ page }) => {
    const { dialog, imports } = await setup(page);
    const csv = "source,target,reading,type,gender,aliases,note,status\r\n" + terms.map(
      ({ source, target }) => `${source},${target},,term,,别名一|别名二,"A ""quoted"", multiline\r\nnote",ok\r\n`,
    ).join("");
    await dialog.locator('input[type="file"]').setInputFiles({
      name: "glossary-export.csv", mimeType: "text/csv", buffer: Buffer.from(csv),
    });
    await expect(page.getByText("Imported terms: 2,183", { exact: true })).toBeVisible();
    await expect(dialog).not.toBeVisible();
    expect(imports).toEqual([terms]);
    await expect(page.getByRole("status").filter({ hasText: "Terms: 2,183" })).toBeVisible();
  });

  test("a CSV whose import data exceeds 5 MiB is rejected in full", async ({ page }) => {
    const { dialog, imports } = await setup(page);
    // Each record fits individually, but JSON escaping brings the total over 5 MiB.
    const note = "\\".repeat(1.5 * 1024 * 1024);
    const csv = "source,target,note\n" + [1, 2].map(i => `Term ${i},译文,${note}\n`).join("");
    await dialog.locator('input[type="file"]').setInputFiles({
      name: "glossary-export.csv", mimeType: "text/csv", buffer: Buffer.from(csv),
    });
    await expect(dialog.getByRole("alert")).toContainText("The parsed glossary exceeds 5 MiB");
    await expect(dialog.getByRole("button", { name: "Choose file" })).toBeVisible();
    expect(imports).toEqual([]);
  });

  test("a file over 5 MiB is rejected before it is read", async ({ page }) => {
    await page.addInitScript(() => {
      FileReader.prototype.readAsText = () => { throw new Error("Oversized file must not be read"); };
    });
    const { dialog, imports } = await setup(page);
    await dialog.locator('input[type="file"]').setInputFiles({
      name: "glossary.json", mimeType: "application/json", buffer: Buffer.alloc(5 * 1024 * 1024 + 1),
    });
    await expect(dialog.getByRole("alert")).toContainText("5 MiB");
    expect(imports).toEqual([]);
  });
}
