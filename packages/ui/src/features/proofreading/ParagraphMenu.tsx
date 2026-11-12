import { useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { Copy, History, Pencil } from "lucide-react";
import { toast } from "../../lib/toast";
import { useI18n } from "../../i18n";
import { copyText } from "./clipboard";

export interface ParagraphMenuAnchor {
  index: number;
  trigger: HTMLButtonElement;
  x: number;
  y: number;
  selection: string;
}

/** Only the active paragraph mounts menu effects and document listeners. */
export function ParagraphMenu({
  anchor,
  source,
  target,
  disabled,
  onOpen,
  onClose,
}: {
  anchor: ParagraphMenuAnchor;
  source: string;
  target: string | null | undefined;
  disabled: boolean;
  onOpen: (index: number, view: "edit" | "history" | "precision") => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const popup = useRef<HTMLDivElement>(null);
  const close = () => {
    onClose();
    if (anchor.trigger.isConnected) anchor.trigger.focus({ preventScroll: true });
  };
  useLayoutEffect(() => {
    const element = popup.current!;
    const rect = element.getBoundingClientRect();
    element.style.left = `${Math.max(8, Math.min(anchor.x, innerWidth - rect.width - 8))}px`;
    element.style.top = `${Math.max(8, Math.min(anchor.y, innerHeight - rect.height - 8))}px`;
    element.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true });
  }, [anchor]);
  useEffect(() => {
    const position = anchor.trigger.getBoundingClientRect();
    const outside = (event: MouseEvent) => {
      if (!popup.current?.contains(event.target as Node)) onClose();
    };
    const scrolled = () => {
      const current = anchor.trigger.getBoundingClientRect();
      // Ignore delayed scroll events from bringing the trigger into view before opening.
      if (current.x !== position.x || current.y !== position.y) onClose();
    };
    // Changing global pointer listeners stalls WebKit on large chapters.
    // Activation events cover mouse, touch and keyboard without that subscription cost.
    window.addEventListener("click", outside, true);
    window.addEventListener("contextmenu", outside, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("scroll", scrolled, true);
    return () => {
      window.removeEventListener("click", outside, true);
      window.removeEventListener("contextmenu", outside, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("scroll", scrolled, true);
    };
  }, [anchor, onClose]);
  const copy = async (text: string) => {
    // Closing now also prevents a slow clipboard write from closing a newer menu.
    close();
    try {
      await copyText(text);
      toast.success(t("proofreading.copied"));
    } catch {
      toast.error(t("proofreading.copyFailed"));
    }
  };
  const open = (view: "edit" | "history" | "precision") => {
    close();
    onOpen(anchor.index, view);
  };
  const item =
    "flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm hover:bg-accent focus:bg-accent focus:outline-none disabled:opacity-40";
  return createPortal(
    <div
      ref={popup}
      role="menu"
      aria-label={t("proofreading.paragraphActions")}
      className="fixed z-50 w-52 max-w-[calc(100vw-16px)] rounded-lg border bg-background p-1 shadow-lg"
      style={{ left: anchor.x, top: anchor.y }}
      onContextMenu={(event) => event.preventDefault()}
      onKeyDown={(event) => {
        const buttons = Array.from(
          popup.current!.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
        );
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? buttons.length - 1
                : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
          buttons[next]?.focus();
        } else if (event.key === "Escape" || event.key === "Tab") {
          event.preventDefault();
          close();
        }
      }}
    >
      <button
        role="menuitem"
        className={item}
        disabled={disabled || target == null}
        onClick={() => open("edit")}
      >
        <Pencil className="h-4 w-4" />
        {t("review.editTranslation")}
      </button>
      <button role="menuitem" className={item} onClick={() => open("history")}>
        <History className="h-4 w-4" />
        {t("proofreading.changeHistory")}
      </button>
      <button role="menuitem" className={item} onClick={() => open("precision")}>
        <History className="h-4 w-4" />
        {t("proofreading.precisionDrafts")}
      </button>
      <div role="separator" className="my-1 border-t" />
      {anchor.selection && (
        <button role="menuitem" className={item} onClick={() => void copy(anchor.selection)}>
          <Copy className="h-4 w-4" />
          {t("proofreading.copySelection")}
        </button>
      )}
      <button
        role="menuitem"
        className={item}
        disabled={target == null}
        onClick={() => void copy(target!)}
      >
        <Copy className="h-4 w-4" />
        {t("proofreading.copyTranslation")}
      </button>
      <button role="menuitem" className={item} onClick={() => void copy(source)}>
        <Copy className="h-4 w-4" />
        {t("proofreading.copySource")}
      </button>
    </div>,
    document.body,
  );
}
