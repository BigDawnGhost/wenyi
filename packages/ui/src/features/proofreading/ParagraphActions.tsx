import { MoreHorizontal } from "lucide-react";
import { useI18n } from "../../i18n";
import type { ParagraphMenuAnchor } from "./ParagraphMenu";

export function ParagraphActions({
  index,
  target,
  expanded,
  onMenu,
}: {
  index: number;
  target: string | null | undefined;
  expanded: boolean;
  onMenu: (anchor: ParagraphMenuAnchor) => void;
}) {
  const { t } = useI18n();
  const show = (trigger: HTMLButtonElement, point?: { x: number; y: number }) => {
    const content = trigger.parentElement!.querySelector("p")!;
    const selection = window.getSelection();
    const selected =
      selection?.rangeCount &&
      selection.getRangeAt(0).intersectsNode(content)
        ? selection.toString()
        : "";
    const rect = trigger.getBoundingClientRect();
    onMenu({
      index,
      trigger,
      x: point?.x ?? rect.right,
      y: point?.y ?? rect.bottom,
      selection: selected,
    });
  };
  return (
    <div
      className="group relative h-full min-w-0"
      onContextMenu={(event) => {
        event.preventDefault();
        show(event.currentTarget.querySelector("button")!, { x: event.clientX, y: event.clientY });
      }}
    >
      <span className="block px-4 pt-4 text-xs text-muted-foreground lg:hidden">
        {t("common.translation")}
      </span>
      <p
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
        type="button"
        aria-label={t("proofreading.paragraphActions")}
        aria-haspopup="menu"
        aria-expanded={expanded}
        className="absolute right-2 top-3 rounded p-1 text-muted-foreground hover:bg-accent focus-visible:ring-1 focus-visible:ring-ring md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100"
        onClick={(event) => show(event.currentTarget)}
        onKeyDown={(event) => {
          if (
            event.key === "ContextMenu" ||
            (event.shiftKey && event.key === "F10")
          ) {
            event.preventDefault();
            show(event.currentTarget);
          }
        }}
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
    </div>
  );
}
