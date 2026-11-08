import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "../../../packages/ui/sandbox/fixtures";
import { chooseOption } from "../../../packages/ui/tests/e2e/select-helper";

for (const fmt of ["epub", "pdf"]) {
  test(`${fmt} projects without BabelDOC state do not offer PDF export`, async ({ page }) => {
    await fakeApi(page, { [`/projects/${pid}`]: { ...project, fmt } });
    await page.goto(`/projects/${pid}/export`);
    await expect(page.getByRole("button", { name: "EPUB", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "PDF", exact: true })).toHaveCount(0);
  });
}

test("BabelDOC PDF export submits without a local rendering engine", async ({ page }) => {
  await fakeApi(page, {
    [`/projects/${pid}`]: {
      ...project, fmt: "pdf", output_formats: [...project.output_formats, "pdf"],
    },
  });
  let submitted: Record<string, unknown> | undefined;
  await page.route(`**/api/projects/${pid}/exports`, async route => {
    if (route.request().method() === "GET") return route.fulfill({ json: [] });
    submitted = route.request().postDataJSON();
    return route.fulfill({ json: { id: 1 } });
  });
  await page.goto(`/projects/${pid}/export`);
  await page.getByRole("button", { name: "PDF", exact: true }).click();
  await page.getByText("Layout and advanced options", { exact: true }).click();
  await expect(page.getByLabel("PDF export engine")).toHaveCount(0);
  await page.getByRole("button", { name: "Generate export", exact: true }).click();
  await expect(page.getByText("Export task submitted", { exact: true })).toBeVisible();
  expect(submitted?.format).toBe("pdf");
  expect(submitted).not.toHaveProperty("pdf_engine");
});

for (const order of ["target_first", "source_first"] as const) {
  test(`bilingual PDF keeps ${order} for automatic and explicit format selection`, async ({ page }) => {
    await fakeApi(page, {
      [`/projects/${pid}`]: {
        ...project, fmt: "pdf", output_formats: [...project.output_formats, "pdf"],
      },
    });
    await page.route(`**/api/projects/${pid}/exports`, async route => {
      if (route.request().method() === "GET") return route.fulfill({ json: [] });
      return route.fulfill({ json: { id: 1 } });
    });
    await page.goto(`/projects/${pid}/export`);
    await page.getByRole("radio", { name: "Bilingual", exact: true }).check();
    await expect(page.getByText(/Each PDF page pairs the original and translated pages side by side/)).toBeVisible();
    await page.getByText("Layout and advanced options", { exact: true }).click();
    const bilingualOrder = page.getByLabel("Bilingual order", { exact: true });
    await expect(bilingualOrder).toHaveText("Translation first");
    const label = order === "target_first" ? "Translation first" : "Source first";
    await chooseOption(bilingualOrder, label);
    await expect(page.getByLabel("Preserve source formatting in bilingual output")).toHaveCount(0);
    await page.getByRole("button", { name: "EPUB", exact: true }).click();
    await expect(bilingualOrder).toHaveText(label);
    for (const format of ["Automatic", "PDF"]) {
      await page.getByRole("button", { name: format, exact: true }).click();
      await expect(bilingualOrder).toHaveText(label);
      const submitted = page.waitForRequest(request =>
        request.method() === "POST" && request.url().endsWith(`/projects/${pid}/exports`),
      );
      await page.getByRole("button", { name: "Generate export", exact: true }).click();
      expect((await submitted).postDataJSON()).toMatchObject({
        ...(format === "PDF" ? { format: "pdf" } : {}),
        bilingual: true, order, preserve_source_style: false,
      });
    }
  });
}
