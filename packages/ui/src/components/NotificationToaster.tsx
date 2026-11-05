import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { Toaster } from "sonner";

/** Mount once in the shared shell, outside the scrolling route content. */
export function NotificationToaster() {
  return createPortal(
    <Toaster
      richColors
      closeButton
      position="top-right"
      offset={24}
      duration={5000}
      className="pointer-events-auto z-[200]"
      icons={{ close: <X className="h-[18px] w-[18px]" aria-hidden="true" /> }}
      toastOptions={{
        classNames: {
          toast: "min-h-16 items-start gap-3 rounded-xl py-5 pl-4 pr-16 text-sm",
          content: "min-w-0",
          icon: "m-0 h-5 w-5 [&_svg]:m-0",
          // Keep Sonner's dismissal behavior, but move the target inside the card.
          closeButton: "left-auto right-2.5 top-2.5 h-11 w-11 transform-none rounded-lg border-0 "
            + "!bg-transparent !text-inherit hover:!bg-black/5 focus-visible:!bg-black/5 active:!bg-black/10",
        },
      }}
    />,
    document.body,
  );
}
