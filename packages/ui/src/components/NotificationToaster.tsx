import { useEffect, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { Toaster } from "sonner";
import { useTheme } from "../theme";

// Bridge Sonner's palette to the same semantic colors as the shared status labels.
const notificationColors = {
  "--normal-bg": "oklch(var(--card))",
  "--normal-border": "oklch(var(--border))",
  "--normal-text": "oklch(var(--card-foreground))",
  "--success-bg": "oklch(var(--status-success))",
  "--success-border": "oklch(var(--status-success-border))",
  "--success-text": "oklch(var(--status-success-foreground))",
  "--error-bg": "oklch(var(--status-danger))",
  "--error-border": "oklch(var(--status-danger-border))",
  "--error-text": "oklch(var(--status-danger-foreground))",
  "--warning-bg": "oklch(var(--status-warning))",
  "--warning-border": "oklch(var(--status-warning-border))",
  "--warning-text": "oklch(var(--status-warning-foreground))",
  "--info-bg": "oklch(var(--status-info))",
  "--info-border": "oklch(var(--status-info-border))",
  "--info-text": "oklch(var(--status-info-foreground))",
} as CSSProperties;

/**
 * True while a page is showing the floating settings action bar. The bar is
 * rendered by the page rather than by the shell, so presence is observed in
 * the document instead of passed down through context.
 */
/**
 * Live width of the sidebar, or 0 when there is none. The toaster is portalled
 * to document.body, which is outside the shell element that carries
 * --sidebar-w, so reading that variable here would only ever return the
 * fallback. Measure the element instead and follow it frame by frame, which also
 * keeps the stack centred while the sidebar animates its own width.
 */
function useSidebarWidth() {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    let frame = 0;
    let last = -1;
    const step = () => {
      const aside = document.querySelector("aside");
      const next = aside ? Math.round(aside.getBoundingClientRect().width) : 0;
      if (next !== last) {
        last = next;
        setWidth(next);
      }
      frame = requestAnimationFrame(step);
    };
    step();
    return () => cancelAnimationFrame(frame);
  }, []);
  return width;
}

function useActionBarPresence() {
  const [present, setPresent] = useState(false);
  useEffect(() => {
    const read = () => setPresent(Boolean(document.querySelector('[data-slot="settings.actions"]')));
    read();
    const observer = new MutationObserver(read);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);
  return present;
}

/** Mount once in the shared shell, outside the scrolling route content. */
export function NotificationToaster() {
  const { theme } = useTheme();
  // Only the settings page floats an action bar over the bottom edge, so only
  // there does the stack need to clear it; elsewhere the offset would leave a
  // toast hovering well above the corner for no reason.
  const barVisible = useActionBarPresence();
  const sidebarWidth = useSidebarWidth();
  // Sonner hard-codes two behaviours that its props do not cover, both of which
  // put it out of step with the rest of the app. Publish them as a style element:
  // the values have to match Sonner's own timers and layout variables rather than
  // the design scale, and a utility class would be generated per build.
  useEffect(() => {
    const tag = document.createElement("style");
    tag.dataset.sonnerExit = "";
    // Sonner hard-codes a 400ms transition on every toast, which is twice the
    // app's own pace (200ms standard, 320ms for dialogs). Its enter, shift and
    // exit then run on that one number: slowing the whole stack, and giving the
    // dismissal a lift-and-collapse that the unmount timer interrupts part-way.
    // Bring the duration onto the same scale and make the exit a fade in place,
    // so leaving costs the same beat as everything else.
    tag.textContent = [
      "[data-sonner-toast][data-removed='true']{--y:none;" +
        "height:var(--initial-height);" +
        "transition:opacity 200ms ease-out}",
    ].join("");
    document.head.append(tag);
    return () => tag.remove();
  }, []);
  return createPortal(
    <Toaster
      theme={theme}
      style={{
        ...notificationColors,
        // Centred on the content pane, not the viewport: the pane starts after the
        // sidebar, so the stack shifts by half the measured width. The width is
        // sampled every frame, so the stack follows the sidebar as it animates
        // instead of arriving ahead of it.
        left: `calc(50% + ${sidebarWidth / 2}px)`,
      }}
      richColors
      closeButton
      // Lower centre, sharing a centre line with the floating action bar: the bar
      // sits at the middle of the content pane, so the stack is shifted by half
      // the sidebar to match. When the bar is present the stack clears it; when
      // it is not, the stack sits at the ordinary corner distance.
      position="bottom-center"
      offset={barVisible ? 92 : 24}
      // Cap the stack. Pinned above the action bar, every extra toast grows the
      // fan the stack opens into, and repeating an action that reports each time
      // fills the whole column. Three is the shape Sonner is designed around.
      visibleToasts={3}
      // Gaps in the stack should not swallow clicks aimed at the page beneath.
      className="pointer-events-none z-[200]"
      duration={5000}
      icons={{ close: <X className="h-4 w-4" aria-hidden="true" /> }}
      toastOptions={{
        classNames: {
          // Sonner removes a dismissed toast on a fixed timer, so a long
          // transition gets cut off mid-flight and reads as a frozen frame
          // followed by a pop. Keep the movement inside that window.
          toast: "pointer-events-none min-h-16 items-start gap-3 rounded-xl py-5 pl-4 pr-16 text-sm "
            + "shadow-e4",
          content: "min-w-0",
          icon: "m-0 h-5 w-5 [&_svg]:m-0",
          // Keep Sonner's dismissal behavior, but move the target inside the card.
          closeButton: "pointer-events-auto left-auto right-2.5 top-2.5 h-11 w-11 transform-none rounded-lg border-0 "
            + "!bg-transparent !text-inherit hover:!bg-hover focus-visible:!bg-hover active:!bg-selected",
        },
      }}
    />,
    document.body,
  );
}
