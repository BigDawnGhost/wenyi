import { expect, type Locator } from "@playwright/test";

/** Resolve the active semantic token in the element's own theme, including portals. */
export async function expectTokenColor(
  locator: Locator,
  property: "color" | "background-color" | "border-top-color",
  token: string,
) {
  const expected = await locator.evaluate((element, token) => {
    const probe = document.createElement("span");
    probe.style.color = `oklch(var(--${token}))`;
    element.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, token);
  await expect(locator).toHaveCSS(property, expected);
}

/** Let the browser convert CSS colors (including OKLCH) to rendered sRGB. */
export async function colorContrast(locator: Locator, first: string, second: string) {
  const pixels = await locator.evaluate((element, colors) => {
    const context = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
    return colors.map(color => {
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color === "color" || color === "background-color"
        ? getComputedStyle(element).getPropertyValue(color)
        : color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
    });
  }, [first, second]);
  const luminance = (rgb: number[]) => {
    const channels = rgb.map(value => {
      const srgb = value / 255;
      return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
    });
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const [a, b] = pixels.map(luminance);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** Status text stays legible while the label surface stays close to its card. */
export async function expectQuietLabelContrast(locator: Locator) {
  const colors = await locator.evaluate(element => {
    const style = getComputedStyle(element);
    const probe = document.createElement("span");
    probe.style.color = "oklch(var(--card))";
    element.append(probe);
    const card = getComputedStyle(probe).color;
    probe.remove();
    return { text: style.color, background: style.backgroundColor, card };
  });
  expect(await colorContrast(locator, colors.text, colors.background)).toBeGreaterThanOrEqual(4.5);
  expect(await colorContrast(locator, colors.background, colors.card)).toBeLessThan(1.5);
}

/** Content needs a themed panel, not merely an input's own background. */
export async function expectCardSurface(locator: Locator) {
  await expect(locator.first()).toBeVisible();
  // Inspect one DOM snapshot: async status messages may appear or disappear together.
  const uncovered = await locator.evaluateAll(elements => elements.filter(element => {
    const probe = document.createElement("span");
    probe.style.color = "oklch(var(--card))";
    element.parentElement!.append(probe);
    const card = getComputedStyle(probe).color;
    probe.remove();
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (parent.matches('[data-slot="page.container"]')) break;
      if (getComputedStyle(parent).backgroundColor === card) return false;
    }
    return true;
  }).map(element => element.id || element.getAttribute("aria-label") || element.tagName));
  expect(uncovered, "Content must be contained by a themed card surface").toEqual([]);
}
