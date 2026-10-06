import { expect, test } from "@playwright/test";
import { fakeApi, pid, project, resolveFixture } from "./fixtures";

test.beforeEach(async ({ page }, info) => {
  if (info.project.name === "desktop") {
    await page.addInitScript(() => {
      Object.assign(window, {
        __WENYI_DESKTOP__: {
          apiBase: "http://127.0.0.1:19485",
          token: "unified-shell-fixture-token",
        },
      });
    });
  }
});

test("shared sandbox project details preserve the metadata from the project list", () => {
  const projects = resolveFixture("GET", "/projects").json as Array<typeof project>;
  for (const summary of projects) {
    const details = resolveFixture("GET", `/projects/${summary.id}`);
    expect(details.status).toBe(200);
    expect(details.json).toMatchObject(summary);
  }
});

for (const width of [375, 800]) {
  test(`chapter header actions fit without overlapping text at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const review = resolveFixture("GET", `/projects/${pid}/review/0`).json as object;
    await fakeApi(
      page,
      { [`/projects/${pid}/review/1`]: { ...review, index: 1, title: "Chapter Two" } },
      info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined,
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

test("project navigation never pairs a new project ID with the previous format", async ({ page }, info) => {
  const book = { ...project, name: "Book project" };
  const subtitles = { ...project, id: "srt-1", name: "Subtitle project", fmt: "srt" };
  await fakeApi(
    page,
    { "/projects": [book, subtitles], [`/projects/${pid}`]: book, "/projects/srt-1": subtitles },
    info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined,
  );
  let release = () => {};
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/projects/srt-1", async route => {
    await pending;
    await route.fulfill({ json: subtitles });
  });
  try {
    await page.goto(`/projects/${pid}`);
    const navigation = page.getByRole("navigation", { name: "Project navigation", exact: true });
    await expect(navigation).toContainText("Book project");
    await expect(navigation.getByRole("link", { name: "Manual proofreading", exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.locator('a[href="/projects/srt-1"]').click();
    await expect(navigation).toContainText("Subtitle project");
    await expect(navigation.getByRole("link", { name: "Subtitle editor", exact: true })).toBeVisible();
    await expect(navigation.getByRole("link", { name: "Manual proofreading", exact: true })).toHaveCount(0);
    release();
    await expect(navigation).toContainText("Subtitle project");
  } finally {
    release();
  }
});

test("source preview closes back to the originating project and restores focus", async ({ page }, info) => {
  await fakeApi(
    page,
    { [`/projects/${pid}`]: { ...project, initialized: false } },
    info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined,
  );
  await page.goto(`/projects/${pid}`);
  const source = page.getByRole("link", { name: "Upload & preview source", exact: true });
  for (const close of ["button", "escape"]) {
    await source.click();
    const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
    await expect(dialog).toBeVisible();
    if (close === "button") {
      await dialog.getByRole("button", { name: "Close", exact: true }).click();
    } else {
      await page.keyboard.press("Escape");
    }
    await expect(page).toHaveURL(`/projects/${pid}`);
    await expect(source).toBeFocused();
  }
});
