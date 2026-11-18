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
  // Sonner dismisses a toast, then unmounts it on a timer that is shorter than
  // its own 400ms exit transition, so the movement is cut off part-way and
  // reads as a frozen frame. Publish a shorter duration for the toast body via
  // a style element: the utility that would express this is generated per
  // build, and this value has to match Sonner's timer, not the design scale.
  useEffect(() => {
    const tag = document.createElement("style");
    tag.dataset.sonnerExit = "";
    tag.textContent =
      "[data-sonner-toast]{transition-duration:180ms,180ms,180ms,150ms}";
    document.head.append(tag);
    return () => tag.remove();
  }, []);
  return createPortal(
    <Toaster
      theme={theme}
      style={{ ...notificationColors, left: "calc(50% + var(--sidebar-w, 240px) / 2)" }}
      richColors
      closeButton
      // Lower centre, sharing a centre line with the floating action bar: the bar
      // sits at the middle of the content pane, so the stack is shifted by half
      // the sidebar to match. When the bar is present the stack clears it; when
      // it is not, the stack sits at the ordinary corner distance.
      position="bottom-center"
      offset={barVisible ? 92 : 24}
      duration={5000}
      className="pointer-events-auto z-[200]"
      icons={{ close: <X className="h-4 w-4" aria-hidden="true" /> }}
      toastOptions={{
        classNames: {
          // Sonner removes a dismissed toast on a fixed timer, so a long
          // transition gets cut off mid-flight and reads as a frozen frame
          // followed by a pop. Keep the movement inside that window.
          toast: "min-h-16 items-start gap-3 rounded-xl py-5 pl-4 pr-16 text-sm "
            + "shadow-e4 ring-1 ring-black/[0.04] dark:ring-white/[0.08]",
          content: "min-w-0",
          icon: "m-0 h-5 w-5 [&_svg]:m-0",
          // Keep Sonner's dismissal behavior, but move the target inside the card.
          closeButton: "left-auto right-2.5 top-2.5 h-11 w-11 transform-none rounded-lg border-0 "
            + "!bg-transparent !text-inherit hover:!bg-hover focus-visible:!bg-hover active:!bg-selected",
        },
      }}
    />,
    document.body,
  );
}
