import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, History, MoreHorizontal, Pencil } from "lucide-react";
import { toast } from "sonner";
import { useI18n } from "@/i18n";
import { copyText } from "./clipboard";

export function ParagraphActions({
  source,
  target,
  disabled,
  onOpen,
}: {
  source: string;
  target: string | null | undefined;
  disabled: boolean;
  onOpen: (view: "edit" | "history") => void;
}) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<{
    x: number;
    y: number;
    selection: string;
  } | null>(null);
  const content = useRef<HTMLParagraphElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const show = (x: number, y: number) => {
    const selection = window.getSelection();
    const selected =
      selection?.rangeCount &&
      content.current &&
      selection.getRangeAt(0).intersectsNode(content.current)
        ? selection.toString()
        : "";
    setMenu({ x, y, selection: selected });
  };
  const close = () => {
    setMenu(null);
    trigger.current?.focus({ preventScroll: true });
  };
  useLayoutEffect(() => {
    if (!menu || !popup.current) return;
    const rect = popup.current.getBoundingClientRect();
    popup.current.style.left = `${Math.max(8, Math.min(menu.x, innerWidth - rect.width - 8))}px`;
    popup.current.style.top = `${Math.max(8, Math.min(menu.y, innerHeight - rect.height - 8))}px`;
    popup.current
      .querySelector<HTMLButtonElement>("button:not(:disabled)")
      ?.focus({ preventScroll: true });
  }, [menu]);
  useEffect(() => {
    if (!menu) return;
    const anchor = trigger.current?.getBoundingClientRect();
    const outside = (event: PointerEvent) => {
      if (!popup.current?.contains(event.target as Node)) setMenu(null);
    };
    const hide = () => setMenu(null);
    const scrolled = () => {
      const current = trigger.current?.getBoundingClientRect();
      // Ignore delayed scroll events from bringing the trigger into view before opening.
      if (current?.x !== anchor?.x || current?.y !== anchor?.y) hide();
    };
    window.addEventListener("pointerdown", outside, true);
    window.addEventListener("resize", hide);
    window.addEventListener("scroll", scrolled, true);
    return () => {
      window.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("resize", hide);
      window.removeEventListener("scroll", scrolled, true);
    };
  }, [menu]);
  const copy = async (text: string) => {
    try {
      await copyText(text);
      toast.success(t("proofreading.copied"));
    } catch {
      toast.error(t("proofreading.copyFailed"));
    }
    close();
  };
  const item =
    "flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm hover:bg-accent focus:bg-accent focus:outline-none disabled:opacity-40";
  return (
    <div
      className="group relative h-full min-w-0"
      onContextMenu={(event) => {
        event.preventDefault();
        show(event.clientX, event.clientY);
      }}
    >
      <span className="block px-4 pt-4 text-xs text-muted-foreground lg:hidden">
        {t("common.translation")}
      </span>
      <p
        ref={content}
        data-testid="translation-text"
        className="select-text whitespace-pre-wrap p-4 pr-10 text-sm leading-relaxed [overflow-wrap:anywhere]"
      >
        {target == null ? (
          <span className="text-muted-foreground">
            {t("proofreading.waitingForTranslation")}
          </span>
        ) : (
          target || (
            <span className="text-muted-foreground">
              {t("review.emptyTranslation")}
            </span>
          )
        )}
      </p>
      <button
        ref={trigger}
        type="button"
        aria-label={t("proofreading.paragraphActions")}
        aria-haspopup="menu"
        aria-expanded={!!menu}
        className="absolute right-2 top-3 rounded p-1 text-muted-foreground hover:bg-accent focus-visible:ring-1 focus-visible:ring-ring md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100"
        onClick={() => {
          const rect = trigger.current!.getBoundingClientRect();
          show(rect.right, rect.bottom);
        }}
        onKeyDown={(event) => {
          if (
            event.key === "ContextMenu" ||
            (event.shiftKey && event.key === "F10")
          ) {
            event.preventDefault();
            const rect = trigger.current!.getBoundingClientRect();
            show(rect.right, rect.bottom);
          }
        }}
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
      {menu &&
        createPortal(
          <div
            ref={popup}
            role="menu"
            aria-label={t("proofreading.paragraphActions")}
            className="fixed z-50 w-52 max-w-[calc(100vw-16px)] rounded-lg border bg-background p-1 shadow-lg"
            style={{ left: menu.x, top: menu.y }}
            onContextMenu={(event) => event.preventDefault()}
            onKeyDown={(event) => {
              const buttons = Array.from(
                popup.current!.querySelectorAll<HTMLButtonElement>(
                  "button:not(:disabled)",
                ),
              );
              const index = buttons.indexOf(
                document.activeElement as HTMLButtonElement,
              );
              if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                event.preventDefault();
                const next =
                  event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? buttons.length - 1
                      : (index +
                          (event.key === "ArrowDown" ? 1 : -1) +
                          buttons.length) %
                        buttons.length;
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
              onClick={() => {
                close();
                onOpen("edit");
              }}
            >
              <Pencil className="h-4 w-4" />
              {t("review.editTranslation")}
            </button>
            <button
              role="menuitem"
              className={item}
              onClick={() => {
                close();
                onOpen("history");
              }}
            >
              <History className="h-4 w-4" />
              {t("proofreading.changeHistory")}
            </button>
            <div role="separator" className="my-1 border-t" />
            {menu.selection && (
              <button
                role="menuitem"
                className={item}
                onClick={() => void copy(menu.selection)}
              >
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
            <button
              role="menuitem"
              className={item}
              onClick={() => void copy(source)}
            >
              <Copy className="h-4 w-4" />
              {t("proofreading.copySource")}
            </button>
          </div>,
          document.body,
        )}
    </div>
  );
}
