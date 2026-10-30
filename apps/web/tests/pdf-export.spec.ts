import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

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
