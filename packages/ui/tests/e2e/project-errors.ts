import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "../../sandbox/fixtures";

export function registerTests() {
  const reason =
    "Model request failed: provider=deepseek operation=translation.body error_type=http_error status=402";
  const failed = { ...project, status: "error", error: reason };
  const healthy = { ...project, id: "healthy-book", name: "Healthy book" };
  const fixtures = {
    "/projects": [failed, healthy],
    [`/projects/${pid}`]: failed,
    [`/projects/${healthy.id}`]: healthy,
  };

  test("a saved project failure appears once inside the translation overview", async ({ page }) => {
    await fakeApi(page, fixtures);
    await page.goto(`/projects/${pid}`);
    await expect(page.getByTitle(project.name, { exact: true })).toBeVisible();
    const notice = page.getByRole("alert").filter({ hasText: reason });
    await expect(notice).toHaveCount(1);
    await expect(
      page.locator('[data-slot="page.container"]').getByRole("alert"),
    ).toHaveText(reason);
    await expect(page.getByRole("button", { name: "Resume task", exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByTitle(project.name, { exact: true })).toBeVisible();
    await expect(notice).toHaveCount(1);
    await expect(notice).toBeVisible();
  });

  for (const remembered of [false, true]) {
    test(`the dashboard does not show the ${remembered ? "remembered" : "default"} project's failure`, async ({
      page,
    }) => {
      if (remembered) {
        await page.addInitScript((pid) => {
          localStorage.setItem("wenyi.lastProject", pid);
        }, pid);
      }
      await fakeApi(page, fixtures);
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "My projects", exact: true })).toBeVisible();
      // Recalling a project for navigation must not recall its error into the page.
      await expect(page.getByTitle(project.name, { exact: true })).toBeVisible();
      await expect(page.getByRole("main").getByText("Failed", { exact: true })).toBeVisible();
      await expect(page.getByText(reason, { exact: true })).toHaveCount(0);
      await expect(page.getByRole("alert")).toHaveCount(0);
    });
  }

  test("project failures stay out of other pages when navigating without reloading", async ({
    page,
  }) => {
    await fakeApi(page, fixtures);
    await page.goto(`/projects/${pid}`);
    await expect(page.getByTitle(project.name, { exact: true })).toBeVisible();
    const notice = page.getByRole("alert").filter({ hasText: reason });
    await expect(notice).toHaveCount(1);

    await page.getByRole("link", { name: "Glossary", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Glossary", exact: true })).toBeVisible();
    await expect(page.getByText(reason, { exact: true })).toHaveCount(0);

    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await expect(page.getByRole("heading", { name: "My projects", exact: true })).toBeVisible();
    await expect(page.getByRole("main").getByText("Failed", { exact: true })).toBeVisible();
    await expect(page.getByText(reason, { exact: true })).toHaveCount(0);

    await page.getByRole("link", { name: "Settings", exact: true }).click();
    await expect(
      page.getByRole("navigation", { name: "Settings navigation", exact: true }),
    ).toBeVisible();
    await expect(page.getByText(reason, { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Back", exact: true }).click();

    await page.getByRole("link", { name: `Open project ${healthy.name}`, exact: true }).click();
    await expect(page.getByRole("heading", { name: healthy.name, exact: true })).toBeVisible();
    await expect(page.getByText(reason, { exact: true })).toHaveCount(0);

    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.getByRole("link", { name: `Open project ${project.name}`, exact: true }).click();
    await expect(notice).toHaveCount(1);
    await expect(notice).toBeVisible();
  });
}
