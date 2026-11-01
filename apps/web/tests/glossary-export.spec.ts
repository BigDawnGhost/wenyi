import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../../packages/ui/tests/e2e/fixtures";

for (const format of ["csv", "json", "apkg"] as const) {
  test(`Web glossary ${format} keeps authenticated browser downloads`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("wenyi_token", "fixture-token"));
    await fakeApi(page);
    let authorization: string | undefined;
    const content = format === "apkg"
      ? Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x00, 0xff])
      : format === "json" ? '[{"source":"文译","target":"Wenyi"}]' : "source,target\n文译,Wenyi\n";
    await page.route(`**/projects/${pid}/glossary/export?format=${format}`, route => {
      authorization = route.request().headers().authorization;
      return route.fulfill({
        body: content,
        headers: { "content-disposition": `attachment; filename=terms.${format}` },
      });
    });
    await page.goto(`/projects/${pid}/glossary?type=person`);
    await page.getByRole("button", { name: "Export", exact: true }).click();
    await expect(page.getByText(/Exports all terms, regardless of filters or selection/)).toHaveCount(0);
    const downloaded = page.waitForEvent("download");
    const label = format === "apkg" ? "Anki deck (.apkg)" : `${format.toUpperCase()} file`;
    await page.getByRole("button", { name: label, exact: true }).click();
    const download = await downloaded;
    expect(download.suggestedFilename()).toBe(`terms.${format}`);
    const stream = await download.createReadStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
    expect(Buffer.concat(chunks)).toEqual(Buffer.from(content));
    expect(authorization).toBe("Bearer fixture-token");
    await expect(page.getByRole("button", { name: "Export", exact: true })).toBeEnabled();
  });
}
