import { expect, test, type Page } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";
import { chooseOption } from "./select-helper";

const dimensions = [
  ["genre", "Genre"],
  ["tone", "Tone"],
  ["narration", "Narration"],
  ["pacing", "Pacing"],
  ["register", "Register"],
  ["dialogue_style", "Dialogue style"],
  ["rhetoric", "Rhetoric"],
] as const;
const original: Record<string, unknown> = {
  ...Object.fromEntries(dimensions.map(([key]) => [key, `Original ${key}`])),
  style_guide: "Original guide",
  book_synopsis: "Original synopsis",
  characters: [{ source: "Alice", target: "艾丽丝", gender: "female", note: "Narrator" }],
  terms: [{ source: "Wonderland", target: "仙境" }],
  custom_metadata: { retained: true },
};

test.beforeEach(async ({ page }, info) => {
  const origin = info.project.name === "desktop" ? "http://127.0.0.1:19485" : undefined;
  if (origin) {
    await page.addInitScript((apiBase) => {
      Object.assign(window, { __WENYI_DESKTOP__: { apiBase, token: "style-fixture-token" } });
    }, origin);
  }
  await fakeApi(page, {}, origin);
});

async function analysisFixture(page: Page) {
  let analysis = structuredClone(original);
  let digest = "Original chapter summary";
  const submitted: Record<string, unknown>[] = [];
  await page.route(`**/projects/${pid}/analysis`, (route) => {
    if (route.request().method() === "PUT") {
      analysis = route.request().postDataJSON().analysis;
      submitted.push(analysis);
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({
      json: {
        analysis,
        chapter_digests: [{ index: 0, title: "Chapter One", digest }],
      },
    });
  });
  await page.route(`**/projects/${pid}/chapter-digests/0`, (route) => {
    digest = route.request().postDataJSON().digest;
    return route.fulfill({ json: { ok: true } });
  });
  return submitted;
}

test("all style dimensions and guidance are editable and persist without losing metadata", async ({
  page,
}) => {
  const submitted = await analysisFixture(page);
  await page.goto(`/projects/${pid}/style`);
  for (const [key, label] of dimensions) {
    const input = page.getByRole("textbox", { name: label, exact: true });
    await expect(input).toHaveValue(`Original ${key}`);
    await input.fill(key === "rhetoric" ? "" : `Updated ${key}\nAdditional guidance`);
  }
  await page.getByRole("textbox", { name: "Style guide", exact: true }).fill("Updated guide");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => submitted.length).toBe(1);
  expect(submitted[0]).toEqual({
    ...original,
    ...Object.fromEntries(dimensions.map(([key]) => [
      key, key === "rhetoric" ? "" : `Updated ${key}\nAdditional guidance`,
    ])),
    style_guide: "Updated guide",
  });
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Register", exact: true }))
    .toHaveValue("Updated register\nAdditional guidance");
  await expect(page.getByRole("textbox", { name: "Rhetoric", exact: true })).toHaveValue("");
  await expect(page.getByRole("textbox", { name: "Style guide", exact: true }))
    .toHaveValue("Updated guide");
});

test("saving summaries does not discard unsaved style or synopsis drafts", async ({ page }) => {
  const submitted = await analysisFixture(page);
  await page.goto(`/projects/${pid}/style`);
  await page.getByRole("textbox", { name: "Tone", exact: true }).fill("Draft tone");
  await page.getByRole("button", { name: "Book synopsis", exact: true }).click();
  await page.getByRole("textbox", { name: "Whole-book synopsis", exact: true }).fill("Draft synopsis");
  await page.getByRole("button", { name: "Chapter summaries", exact: true }).click();
  await page.getByRole("button", { name: "Original chapter summary", exact: true }).click();
  await page.getByRole("textbox", { name: "Chapter 1 summary", exact: true }).fill("Saved summary");
  await page.getByRole("button", { name: "Save summary", exact: true }).click();
  await expect(page.getByRole("button", { name: "Saved summary", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Book synopsis", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Whole-book synopsis", exact: true }))
    .toHaveValue("Draft synopsis");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => submitted.length).toBe(1);
  expect(submitted[0].tone).toBe(original.tone);
  await page.getByRole("button", { name: "Style analysis", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Tone", exact: true })).toHaveValue("Draft tone");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => submitted.length).toBe(2);
  expect(submitted[1].book_synopsis).toBe("Draft synopsis");
  expect(submitted[1].tone).toBe("Draft tone");
});

test("style and synopsis drafts survive visiting the glossary and going back", async ({
  page,
}) => {
  await analysisFixture(page);
  await page.route(`**/projects/${pid}/glossary/terms*`, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(`**/projects/${pid}/glossary/conflicts`, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto(`/projects/${pid}/style`);
  await page.getByRole("textbox", { name: "Tone", exact: true }).fill("Unsaved tone");
  await page.getByRole("button", { name: "Book synopsis", exact: true }).click();
  await page.getByRole("textbox", { name: "Whole-book synopsis", exact: true }).fill("Unsaved synopsis");
  await page.getByRole("link", { name: "Glossary", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/glossary$`));
  await page.goBack();
  await page.getByRole("button", { name: "Style analysis", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Tone", exact: true })).toHaveValue("Unsaved tone");
  await page.getByRole("button", { name: "Book synopsis", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Whole-book synopsis", exact: true }))
    .toHaveValue("Unsaved synopsis");
});

test("a pending analysis save stays read-only after glossary navigation and Back", async ({
  page,
}) => {
  const submitted = await analysisFixture(page);
  await page.route(`**/projects/${pid}/glossary/terms*`, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(`**/projects/${pid}/glossary/conflicts`, (route) =>
    route.fulfill({ json: [] }),
  );
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let puts = 0;
  await page.route(`**/projects/${pid}/analysis`, async (route) => {
    if (route.request().method() === "PUT") {
      puts++;
      await pending;
    }
    return route.fallback();
  });
  // Load both routes first so navigation unmounts the editor rather than suspending.
  await page.goto(`/projects/${pid}/glossary`);
  await expect(page.getByRole("heading", { name: "Glossary", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Style & synopsis", exact: true }).click();
  await page.getByRole("textbox", { name: "Tone", exact: true }).fill("First saved tone");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => puts).toBe(1);
  try {
    await page.getByRole("link", { name: "Glossary", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Glossary", exact: true })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("textbox", { name: "Tone", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
    expect(puts).toBe(1);
  } finally {
    release();
  }
  await expect.poll(() => submitted.length).toBe(1);
  await expect(page.getByRole("textbox", { name: "Tone", exact: true })).toBeEnabled();
  await page.getByRole("textbox", { name: "Tone", exact: true }).fill("Second saved tone");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => submitted.length).toBe(2);
  expect(submitted[1].tone).toBe("Second saved tone");
});

test("failed saves retain drafts for retry and busy projects remain read-only", async ({ page }) => {
  const submitted = await analysisFixture(page);
  let fail = true;
  await page.route(`**/projects/${pid}/analysis`, (route) =>
    fail && route.request().method() === "PUT"
      ? route.fulfill({ status: 409, json: { detail: "Project is busy" } })
      : route.fallback(),
  );
  await page.goto(`/projects/${pid}/style`);
  await page.getByRole("textbox", { name: "Genre", exact: true }).fill("Retry draft");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Project is busy");
  await expect(page.getByRole("textbox", { name: "Genre", exact: true })).toHaveValue("Retry draft");
  fail = false;
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => submitted.length).toBe(1);
  expect(submitted[0].genre).toBe("Retry draft");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("textbox", { name: "Tone", exact: true }).fill("Unsaved tone");
  await page.route(`**/projects/${pid}`, (route) =>
    route.fulfill({ json: { ...project, status: "translating" } }),
  );
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled({ timeout: 8000 });
  for (const [, label] of dimensions) {
    await expect(page.getByRole("textbox", { name: label, exact: true })).toBeDisabled();
  }
  await page.getByRole("button", { name: "Book synopsis", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Whole-book synopsis", exact: true })).toBeDisabled();
});

test("style has no character shortcut and the sidebar glossary retains person filtering", async ({ page }) => {
  await analysisFixture(page);
  let requestedType: string | null = null;
  await page.route(`**/projects/${pid}/glossary/terms*`, (route) => {
    requestedType = new URL(route.request().url()).searchParams.get("type");
    return route.fulfill({
      json: [
        { source: "Alice", target: "艾丽丝", type: "person", aliases: [] },
        ...(requestedType === "person"
          ? []
          : [{ source: "Rabbit hole", target: "兔子洞", type: "term", aliases: [] }]),
      ],
    });
  });
  await page.route(`**/projects/${pid}/glossary/conflicts`, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto(`/projects/${pid}/style`);
  await expect(page.getByRole("heading", { name: "Style & synopsis", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Characters", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Manage characters in glossary", exact: true }))
    .toHaveCount(0);
  await page.getByRole("link", { name: "Glossary", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Type", exact: true })).toContainText("All types");
  await chooseOption(page.getByRole("combobox", { name: "Type", exact: true }), "Person");
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/glossary\\?type=person$`));
  await expect.poll(() => requestedType).toBe("person");
  await expect(page.getByRole("combobox", { name: "Type", exact: true })).toContainText("Person");
  await expect(page.getByText("Alice", { exact: true })).toBeVisible();
  await chooseOption(page.getByRole("combobox", { name: "Type", exact: true }), "All types");
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/glossary$`));
  await expect(page.getByText("Rabbit hole", { exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/projects/${pid}/glossary\\?type=person$`));
  await expect(page.getByRole("combobox", { name: "Type", exact: true })).toContainText("Person");
  await expect(page.getByText("Rabbit hole", { exact: true })).toHaveCount(0);
  await page.goto(`/projects/${pid}/glossary?type=not-a-term-type`);
  await expect(page.getByRole("combobox", { name: "Type", exact: true })).toContainText("All types");
  await expect.poll(() => requestedType).toBeNull();
});
