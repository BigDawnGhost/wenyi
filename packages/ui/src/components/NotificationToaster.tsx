import type { CSSProperties } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { Toaster } from "sonner";
import { useTheme } from "../theme";

// Bridge Sonner's palette to the same semantic colors as the shared status labels.
const notificationColors = {
  "--normal-bg": "hsl(var(--card))",
  "--normal-border": "hsl(var(--border))",
  "--normal-text": "hsl(var(--card-foreground))",
  "--success-bg": "hsl(var(--status-success))",
  "--success-border": "hsl(var(--status-success-border))",
  "--success-text": "hsl(var(--status-success-foreground))",
  "--error-bg": "hsl(var(--status-danger))",
  "--error-border": "hsl(var(--status-danger-border))",
  "--error-text": "hsl(var(--status-danger-foreground))",
  "--warning-bg": "hsl(var(--status-warning))",
  "--warning-border": "hsl(var(--status-warning-border))",
  "--warning-text": "hsl(var(--status-warning-foreground))",
  "--info-bg": "hsl(var(--status-info))",
  "--info-border": "hsl(var(--status-info-border))",
  "--info-text": "hsl(var(--status-info-foreground))",
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
