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
 * Live width of the sidebar, or 0 when there is none. The toaster is portalled
 * to document.body, which is outside the shell element that carries
 * --sidebar-w. Observe the element's actual width to follow the animation
 * without scheduling work while the sidebar is still.
 */
function useSidebarWidth() {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const aside = document.querySelector('[data-slot="sidebar"]');
    if (!aside) return;
    const read = () => setWidth(aside.getBoundingClientRect().width);
    const observer = new ResizeObserver(read);
    read();
    observer.observe(aside, { box: "border-box" });
    return () => observer.disconnect();
  }, []);
  return width;
}

/** Mount once in the shared shell, outside the scrolling route content. */
export function NotificationToaster() {
  const { theme } = useTheme();
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
        // sidebar, so the stack shifts by half the measured width, including
        // while the sidebar animates.
        left: `calc(50% + ${sidebarWidth / 2}px)`,
      }}
      richColors
      closeButton
      position="bottom-center"
      offset={24}
      // Cap the stack so repeated notifications do not fill the content pane.
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
