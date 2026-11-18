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
  // Three behaviours Sonner decides internally rather than from its props.
  // They are published as a style element because the values have to match
  // Sonner's own timers and layout variables, not the design scale.
  useEffect(() => {
    const tag = document.createElement("style");
    tag.dataset.sonnerExit = "";
    // Sonner opens the stack on hover by switching each toast to a different
    // layout, so the swept-back ones jump to full height and a new offset. On a
    // bottom-anchored stack that throws them up the screen, and a toast
    // dismissed meanwhile animates against a layout that is still moving. Pin
    // the expanded state to the resting layout so hovering changes nothing.
    tag.textContent = [
      "[data-sonner-toast]{transition-duration:180ms,180ms,180ms,150ms}",
      "[data-sonner-toast][data-expanded='true'][data-front='false']{" +
        "--scale:calc(var(--toasts-before) * 0.05 + 1);" +
        "--y:translateY(calc(var(--lift-amount) * var(--toasts-before)))" +
        " scale(calc(-1 * var(--scale)));" +
        "height:var(--front-toast-height)}",
      "[data-sonner-toast][data-expanded='true'][data-front='false']" +
        "[data-styled='true'] > *{opacity:0}",
      // Exit as a fade in place. Sonner slides a dismissed toast off in its
      // lift direction while collapsing its height, and the two run on
      // different clocks, so the box was seen shrinking and drifting before it
      // vanished. Holding position and height leaves only the opacity change.
      "[data-sonner-toast][data-removed='true']{--y:none;" +
        "height:var(--initial-height);" +
        "transition:opacity 180ms ease-out}",
    ].join("");
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

      className="pointer-events-auto z-[200]"
      duration={5000}
      icons={{ close: <X className="h-4 w-4" aria-hidden="true" /> }}
      toastOptions={{
        classNames: {
          // Sonner removes a dismissed toast on a fixed timer, so a long
          // transition gets cut off mid-flight and reads as a frozen frame
          // followed by a pop. Keep the movement inside that window.
          toast: "min-h-16 items-start gap-3 rounded-xl py-5 pl-4 pr-16 text-sm "
            + "shadow-e4",
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
