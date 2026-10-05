import { toast as sonner, type ExternalToast } from "sonner";

/**
 * Button feedback appears directly under the button that triggered it,
 * wherever the button lives: horizontally centred by default, hugging the left
 * or right edge of the content area when centring would overflow it.
 *
 * - A capture-phase click records the trigger rectangle.
 * - Vertical placement (10px below the button) is set with the toast itself.
 * - Horizontal placement needs the rendered toast width, so a rAF pass right
 *   after the toast mounts recentres it before the first paint, in the
 *   scrolling content space of [data-slot="content"] (index.css: right 24,
 *   top 62 of the Toaster container, scrollbar excluded).
 * Clicks older than a few seconds, or targets outside the content area, keep
 * the default corner position.
 */
interface Trigger {
  rect: DOMRect;
  at: number;
}
let lastTrigger: Trigger | null = null;

if (typeof document !== "undefined") {
  document.addEventListener(
    "click",
    (event) => {
      const el = (event.target as HTMLElement | null)?.closest?.("button");
      if (el?.closest('[data-slot="content"]')) {
        lastTrigger = { rect: el.getBoundingClientRect(), at: Date.now() };
      } else {
        lastTrigger = null;
      }
    },
    true,
  );
}

/**
 * Alignment depends only on where the button sits inside the content area
 * (thirds), never on the toast width — every toast at the same button must
 * align the same way, or a long toast and a short one would flip between
 * right-aligned and centred. The thirds keep a max-width toast inside the
 * content area in every zone.
 */
function alignZone(
  button: DOMRect,
  contentLeft: number,
  contentRight: number,
): "left" | "centre" | "right" {
  const width = contentRight - contentLeft;
  const offset = button.left + button.width / 2 - contentLeft;
  if (offset >= (width * 2) / 3) return "right";
  if (offset < width / 3) return "left";
  return "centre";
}

function anchorTop(): number | undefined {
  if (typeof window === "undefined" || !window.matchMedia("(min-width: 689px)").matches)
    return undefined;
  if (!lastTrigger || Date.now() - lastTrigger.at > 5000) return undefined;
  const main = document.querySelector<HTMLElement>('[data-slot="content"]');
  if (!main) return undefined;
  const containerTop = main.getBoundingClientRect().top + 62 - main.scrollTop;
  return Math.round(lastTrigger.rect.bottom + 10 - containerTop);
}

function refineAnchor(message: string, attempt = 0): void {
  const retry = () => {
    // The Toaster container mounts together with the very first toast, so keep
    // trying instead of giving up before it exists (timers also survive a
    // throttled rAF in a hidden window).
    if (attempt < 20) setTimeout(() => refineAnchor(message, attempt + 1), 30);
  };
  const step = () => {
    const trigger = lastTrigger;
    if (!trigger || Date.now() - trigger.at > 5000) return;
    const main = document.querySelector<HTMLElement>('[data-slot="content"]');
    const container = document.querySelector("[data-sonner-toaster]");
    if (!main || !container) {
      retry();
      return;
    }
    const li = [...container.querySelectorAll("li[data-sonner-toast]")].find(
      (el) => el.textContent?.trim() === message.trim(),
    ) as HTMLElement | undefined;
    if (!li) {
      retry();
      return;
    }
    const width = li.getBoundingClientRect().width || 120;
    const button = trigger.rect;
    const box = main.getBoundingClientRect();
    const scrollbar = main.offsetWidth - main.clientWidth;
    const contentLeft = box.left;
    const contentRight = box.right - scrollbar;
    // The Toaster container is a fixed-width box: offsets must be relative to
    // its measured edges, not to the anchor point at its right edge.
    const contRect = container.getBoundingClientRect();
    const centre = button.left + button.width / 2;
    const zone = alignZone(button, contentLeft, contentRight);
    let left: number | string;
    let right: number | string;
    if (zone === "right") {
      // Right third: flush with the button right edge for every toast.
      right = Math.round(contRect.right - button.right);
      left = "auto";
    } else if (zone === "left") {
      // Left third: grow inward from the button left edge — never under the sidebar.
      left = Math.round(button.left - contRect.left);
      right = "auto";
    } else {
      // Middle third: centred on the button.
      left = Math.round(centre - width / 2 - contRect.left);
      right = "auto";
    }
    li.style.left = typeof left === "number" ? `${left}px` : left;
    li.style.right = typeof right === "number" ? `${right}px` : right;
  };
  requestAnimationFrame(step);
}

function preHorizontal(button: DOMRect): { left: number; right: string } | { right: number; left: string } | undefined {
  const main = document.querySelector<HTMLElement>('[data-slot="content"]');
  if (!main) return undefined;
  const box = main.getBoundingClientRect();
  const scrollbar = main.offsetWidth - main.clientWidth;
  const contentLeft = box.left;
  const contentRight = box.right - scrollbar;
  // First frame only: the container may not exist yet for the very first
  // toast; approximate its box with the default 356px width. The refine pass
  // snaps to the measured container and the real toast width.
  const container = document.querySelector("[data-sonner-toaster]");
  const contRect = container?.getBoundingClientRect();
  const containerLeft = contRect ? contRect.left : contentRight - 24 - 356;
  const containerRight = contRect ? contRect.right : contentRight - 24;
  const centre = button.left + button.width / 2;
  const guess = 160;
  const zone = alignZone(button, contentLeft, contentRight);
  if (zone === "right")
    return { right: Math.round(containerRight - button.right), left: "auto" };
  if (zone === "left")
    return { left: Math.round(button.left - containerLeft), right: "auto" };
  return { left: Math.round(centre - guess / 2 - containerLeft), right: "auto" };
}

function anchored(message: string, options?: ExternalToast): ExternalToast | undefined {
  const top = anchorTop();
  if (!top || !lastTrigger) return options;
  const pre = preHorizontal(lastTrigger.rect);
  // rAF refines before first paint; the timer fallback covers throttled rAF
  // (hidden window) — both passes are idempotent.
  requestAnimationFrame(() => refineAnchor(message));
  setTimeout(() => refineAnchor(message), 50);
  return { ...options, style: { ...options?.style, ...(pre ? {
    left: typeof pre.left === "number" ? `${pre.left}px` : pre.left,
    right: typeof pre.right === "number" ? `${pre.right}px` : pre.right,
  } : {}), top: `${top}px` } };
}

export const toast = {
  success: (message: string, options?: ExternalToast) =>
    sonner.success(message, anchored(message, options)),
  error: (message: string, options?: ExternalToast) =>
    sonner.error(message, anchored(message, options)),
  info: (message: string, options?: ExternalToast) =>
    sonner.info(message, anchored(message, options)),
  warning: (message: string, options?: ExternalToast) =>
    sonner.warning(message, anchored(message, options)),
  message: (message: string, options?: ExternalToast) =>
    sonner.message(message, anchored(message, options)),
};
