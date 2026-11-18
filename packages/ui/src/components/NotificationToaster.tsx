import type { CSSProperties } from "react";
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

/** Mount once in the shared shell, outside the scrolling route content. */
export function NotificationToaster() {
  const { theme } = useTheme();
  return createPortal(
    <Toaster
      theme={theme}
      style={notificationColors}
      richColors
      closeButton
      position="top-right"
      offset={24}
      duration={5000}
      className="pointer-events-auto z-[200]"
      icons={{ close: <X className="h-4.5 w-4.5" aria-hidden="true" /> }}
      toastOptions={{
        classNames: {
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
