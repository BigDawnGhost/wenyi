import { expect, test } from "@playwright/test";
import { fakeApi, pid, project, resolveFixture } from "../../sandbox/fixtures";


/** Whether the Desktop host must keep running this suite as well.
 *
 * `registerShellTests` is the host contract suite: it resolves fixtures directly and asserts how the host wires `apiOrigin` into the shared shell, which is the seam the Desktop transport replaces.
 */
export const desktopHostSensitive = true;

/** Hosts register these checks after installing their own bootstrap hooks. */
export function registerShellTests({ apiOrigin }: { apiOrigin?: string } = {}) {
  test("shared sandbox project details preserve the metadata from the project list", () => {
    const projects = resolveFixture("GET", "/projects").json as Array<typeof project>;
    for (const summary of projects) {
      const details = resolveFixture("GET", `/projects/${summary.id}`);
      expect(details.status).toBe(200);
      expect(details.json).toMatchObject(summary);
    }
  });

  test("API fixtures leave nested api source paths to the asset server", async ({ page }) => {
    const source = "export const fixtureAsset = true;";
    await page.route("**/assets/api/client.js", route => route.fulfill({
      contentType: "text/javascript",
      body: source,
    }));
    // Register last: a broad API glob would shadow the asset route above.
    await fakeApi(page, {}, apiOrigin);
    await page.goto("/");
    const actual = await page.evaluate(async () => {
      const response = await fetch("/assets/api/client.js");
      return response.text();
    });
    expect(actual).toBe(source);
  });

  for (const width of [375, 800]) {
    test(`chapter header actions fit without overlapping text at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      const review = resolveFixture("GET", `/projects/${pid}/review/0`).json as object;
      await fakeApi(
        page,
        { [`/projects/${pid}/review/1`]: { ...review, index: 1, title: "Chapter Two" } },
        apiOrigin,
      );
      await page.goto(`/projects/${pid}/proofreading/1`);
      const header = page.locator('[data-slot="page.header"]');
      await expect(header.getByRole("heading")).toHaveText("Manual proofreading — Chapter Two");
      await expect(header.getByRole("button")).toHaveCount(3);
      const geometry = await header.evaluate(element => {
        const box = element.getBoundingClientRect();
        const text = [...element.querySelectorAll("h1,p")].map(node => node.getBoundingClientRect());
        return [...element.querySelectorAll("button")].map(button => {
          const bounds = button.getBoundingClientRect();
          return {
            label: button.textContent,
            clipped: bounds.left < box.left || bounds.right > box.right,
            overlaps: text.some(rect =>
              bounds.left < rect.right && bounds.right > rect.left &&
              bounds.top < rect.bottom && bounds.bottom > rect.top),
          };
        });
      });
      expect(geometry.every(button => !button.clipped && !button.overlaps), JSON.stringify(geometry))
        .toBe(true);
    });
  }

  test("project navigation follows the selected project while details load", async ({ page }) => {
    const book = { ...project, name: "Book project" };
    const nextBook = { ...project, id: "book-next", name: "Next book", fmt: "docx" };
    await fakeApi(
      page,
      { "/projects": [book, nextBook], [`/projects/${pid}`]: book, "/projects/book-next": nextBook },
      apiOrigin,
    );
    let release = () => {};
    const pending = new Promise<void>(resolve => { release = resolve; });
    await page.route("**/projects/book-next", async route => {
      await pending;
      await route.fulfill({ json: nextBook });
    });
    try {
      await page.goto(`/projects/${pid}`);
      const navigation = page.getByRole("navigation", { name: "Project navigation", exact: true });
      await expect(navigation).toContainText("Book project");
      await expect(navigation.getByRole("link", { name: "Manual proofreading", exact: true })).toBeVisible();
      await page.getByRole("link", { name: "Projects", exact: true }).click();
      await page.locator('a[href="/projects/book-next"]').click();
      await expect(navigation).toContainText("Next book");
      await expect(navigation.getByRole("link", { name: "Manual proofreading", exact: true })).toHaveAttribute(
        "href", "/projects/book-next/proofreading",
      );
      release();
      await expect(navigation).toContainText("Next book");
    } finally {
      release();
    }
  });

  test("creation closes back to the originating project and restores sidebar focus", async ({ page }) => {
    await fakeApi(
      page,
      { [`/projects/${pid}`]: { ...project, initialized: false } },
      apiOrigin,
    );
    await page.goto(`/projects/${pid}`);
    const create = page.getByRole("link", { name: "Create project", exact: true });
    for (const close of ["button", "escape"]) {
      await create.click();
      const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
      await expect(dialog).toBeVisible();
      if (close === "button") {
        await dialog.getByRole("button", { name: "Close", exact: true }).click();
      } else {
        await page.keyboard.press("Escape");
      }
      await expect(page).toHaveURL(`/projects/${pid}`);
      await expect(create).toBeFocused();
    }
  });
}
