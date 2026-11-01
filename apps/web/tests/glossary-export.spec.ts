import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "../../../packages/ui/tests/e2e/fixtures";

for (const format of ["csv", "json"] as const) {
  test(`Web glossary ${format} keeps authenticated browser downloads`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("wenyi_token", "fixture-token"));
    await fakeApi(page);
    let authorization: string | undefined;
    const content = format === "json" ? '[{"source":"文译","target":"Wenyi"}]' : "source,target\n文译,Wenyi\n";
    await page.route(`**/projects/${pid}/glossary/export?format=${format}`, route => {
      authorization = route.request().headers().authorization;
      return route.fulfill({
        body: content,
        headers: { "content-disposition": `attachment; filename=terms.${format}` },
      });
    });
    await page.goto(`/projects/${pid}/glossary`);
    await page.getByRole("button", { name: "Export", exact: true }).click();
    const downloaded = page.waitForEvent("download");
    await page.getByRole("button", { name: `${format.toUpperCase()} file`, exact: true }).click();
    expect((await downloaded).suggestedFilename()).toBe(`terms.${format}`);
    expect(authorization).toBe("Bearer fixture-token");
    await expect(page.getByRole("button", { name: "Export", exact: true })).toBeEnabled();
  });
}
