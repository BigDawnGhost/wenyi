import { createPortal } from "react-dom";
import { Toaster } from "sonner";

/** Mount once in the shared shell, outside the scrolling route content. */
export function NotificationToaster() {
  return createPortal(
    <Toaster
      richColors
      closeButton
      position="top-right"
      offset={24}
      mobileOffset={16}
      duration={5000}
      style={{ zIndex: 200, pointerEvents: "auto" }}
    />,
    document.body,
  );
}
