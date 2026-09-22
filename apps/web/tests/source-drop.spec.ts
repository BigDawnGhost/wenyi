import { expect, test, type Page } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

async function fileTransfer(
  page: Page,
  files: { name: string; content?: string }[],
) {
  return page.evaluateHandle((items) => {
    const transfer = new DataTransfer();
    for (const item of items) {
      transfer.items.add(
        new File([item.content ?? "source fixture"], item.name, {
          type: "application/octet-stream",
        }),
      );
    }
    return transfer;
  }, files);
}

for (const chinese of [false, true]) {
  test(`dropping a source uses the existing upload flow in ${chinese ? "Chinese" : "English"}`, async ({
    page,
  }, testInfo) => {
    await page.addInitScript(
      (locale) => localStorage.setItem("wenyi.locale", locale),
      chinese ? "zh-CN" : "en",
    );
    await fakeApi(page);
    const uploads: {
      name: string;
      content: string;
      project: Record<string, unknown>;
    }[] = [];
    await page.route("**/api/projects", async (route) => {
      const form = await new Response(
        new Uint8Array(route.request().postDataBuffer()!),
        {
          headers: {
            "content-type": route.request().headers()["content-type"],
          },
        },
      ).formData();
      const file = form.get("file") as File;
      uploads.push({
        name: file.name,
        content: await file.text(),
        project: JSON.parse(String(form.get("project"))),
      });
      await route.fulfill({
        status: 503,
        json: { detail: "Upload unavailable" },
      });
    });
    await page.goto("/projects/new");
    await page
      .getByLabel(chinese ? "项目名称" : "Project name", { exact: true })
      .fill("Dropped book");
    const zone = page.getByRole("group", {
      name: chinese ? "原文文件选择区域" : "Source file selection",
    });
    const browse = zone.getByRole("button", {
      name: chinese ? "浏览文件" : "Browse files",
    });
    const file = await fileTransfer(page, [
      { name: "bản thảo.docx", content: "The selected source" },
    ]);
    const release = zone.getByText(
      chinese ? "松开以选择原文文件。" : "Release to select the source file.",
    );
    await zone.dispatchEvent("dragenter", { dataTransfer: file });
    await zone.dispatchEvent("dragover", { dataTransfer: file });
    await expect(release).toBeVisible();
    await browse.dispatchEvent("dragenter", { dataTransfer: file });
    await zone.dispatchEvent("dragleave", { dataTransfer: file });
    await expect(release).toBeVisible();
    await browse.dispatchEvent("dragleave", { dataTransfer: file });
    await expect(release).toHaveCount(0);
    await zone.dispatchEvent("dragenter", { dataTransfer: file });
    await zone.dispatchEvent("drop", { dataTransfer: file });
    await file.dispose();
    await expect(release).toHaveCount(0);
    await expect(
      zone.getByText("bản thảo.docx", { exact: true }),
    ).toBeVisible();
    expect(uploads).toHaveLength(0);
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page
        .getByRole("main")
        .evaluate((element) => element.scrollWidth <= element.clientWidth),
    ).toBe(true);
    await zone.screenshot({
      path: testInfo.outputPath("source-drop-zone.png"),
    });
    await page
      .getByRole("button", {
        name: chinese ? "创建项目" : "Create project",
        exact: true,
      })
      .click();
    await expect(page.getByText("503: Upload unavailable")).toBeVisible();
    expect(uploads).toEqual([
      {
        name: "bản thảo.docx",
        content: "The selected source",
        project: expect.objectContaining({ name: "Dropped book" }),
      },
    ]);
    const retry = await fileTransfer(page, [{ name: "next.docx" }]);
    await zone.dispatchEvent("drop", { dataTransfer: retry });
    await retry.dispose();
    await expect(page.getByText("503: Upload unavailable")).toHaveCount(0);
    await expect(zone.getByText("next.docx", { exact: true })).toBeVisible();
  });
}

test("drops validate files and reject multiple files without replacing the selection", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("Dropped book");
  const zone = page.getByRole("group", { name: "Source file selection" });
  const create = page.getByRole("button", {
    name: "Create project",
    exact: true,
  });
  const drop = async (files: { name: string; content?: string }[]) => {
    const dataTransfer = await fileTransfer(page, files);
    await zone.dispatchEvent("drop", { dataTransfer });
    await dataTransfer.dispose();
  };
  await drop([{ name: "empty.docx", content: "" }]);
  await expect(
    page.getByText("The source file is empty. Choose a file with content."),
  ).toBeVisible();
  await expect(create).toBeDisabled();
  await drop([{ name: "archive.zip" }]);
  await expect(
    page.getByText("This file format is not supported."),
  ).toBeVisible();
  await expect(create).toBeDisabled();
  await drop([{ name: "book.PDF" }]);
  await expect(page.getByLabel("PDF parser", { exact: true })).toBeVisible();
  await expect(create).toBeEnabled();
  await drop([{ name: "one.docx" }, { name: "two.docx" }]);
  await expect(zone.getByRole("alert")).toHaveText(
    "Choose one source file at a time.",
  );
  await expect(zone.getByText("book.PDF", { exact: true })).toBeVisible();
  const text = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.setData("text/plain", "not a source file");
    return data;
  });
  await zone.dispatchEvent("dragenter", { dataTransfer: text });
  await expect(
    zone.getByText("Release to select the source file."),
  ).toHaveCount(0);
  await zone.dispatchEvent("drop", { dataTransfer: text });
  await text.dispose();
  await expect(zone.getByText("book.PDF", { exact: true })).toBeVisible();
  await drop([{ name: "movie.srt" }]);
  await expect(zone.getByRole("alert")).toHaveCount(0);
  await expect(page.getByLabel("PDF parser", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: "Prepare before translating" }),
  ).toHaveCount(0);
  await expect(create).toBeEnabled();
});

test("uploading and created projects reject replacement drops", async ({
  page,
}) => {
  await fakeApi(page);
  let finish: () => void = () => {};
  const hold = new Promise<void>((resolve) => {
    finish = resolve;
  });
  await page.route("**/api/projects", async (route) => {
    await hold;
    await route.fulfill({ json: { ...project, status: "parsing" } });
  });
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("Dropped book");
  const zone = page.getByRole("group", { name: "Source file selection" });
  const original = await fileTransfer(page, [{ name: "original.docx" }]);
  await zone.dispatchEvent("drop", { dataTransfer: original });
  await original.dispose();
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  const replacement = await fileTransfer(page, [{ name: "replacement.docx" }]);
  try {
    await expect(zone).toHaveAttribute("aria-disabled", "true");
    await expect(
      zone.getByRole("button", { name: "Browse files" }),
    ).toBeDisabled();
    await zone.dispatchEvent("dragenter", { dataTransfer: replacement });
    await expect(
      zone.getByText("Release to select the source file."),
    ).toHaveCount(0);
    await zone.dispatchEvent("drop", { dataTransfer: replacement });
    await expect(
      zone.getByText("original.docx", { exact: true }),
    ).toBeVisible();
    finish();
    await expect(page).toHaveURL(`/projects/new?project=${pid}`);
    await zone.dispatchEvent("drop", { dataTransfer: replacement });
    await expect(
      zone.getByText("original.docx", { exact: true }),
    ).toBeVisible();
    await expect(zone).toHaveAttribute("aria-disabled", "true");
  } finally {
    finish();
    await replacement.dispose();
  }
});
