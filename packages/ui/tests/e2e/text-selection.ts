import type { Locator, Page } from "@playwright/test";

type Point = { x: number; y: number };

/** Use the rendered glyph boxes to aim the mouse, not to set the selection. */
export async function textPoint(locator: Locator, edge: "start" | "end" | "middle") {
  return locator.evaluate((element, edge) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    const boxes: DOMRect[] = [];
    while (walker.nextNode()) {
      range.selectNodeContents(walker.currentNode);
      boxes.push(...[...range.getClientRects()].filter(box => box.width > 0));
    }
    const box = edge === "end" ? boxes.at(-1) : boxes[0];
    if (!box) throw new Error(`No rendered text in ${element.outerHTML}`);
    const x = edge === "start" ? box.left + 0.5
      : edge === "end" ? box.right - 0.5
      : box.left + box.width / 2;
    return { x, y: box.top + box.height / 2 };
  }, edge);
}

export async function dragSelection(page: Page, start: Point, end: Point) {
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(end.x, end.y, { steps: 12 });
  await page.mouse.up();
  return selectedText(page);
}

export async function dragText(locator: Locator) {
  await locator.scrollIntoViewIfNeeded();
  return dragSelection(locator.page(), await textPoint(locator, "start"), await textPoint(locator, "end"));
}

export async function selectedText(page: Page) {
  return page.evaluate(() => window.getSelection()?.toString().trim() ?? "");
}
