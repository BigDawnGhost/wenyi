import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { defaultRangeExtractor, useVirtualizer } from "@tanstack/react-virtual";
import { useSearchParams } from "react-router-dom";
import type { ChapterWindow, ReaderSegment } from "../../api/types";
import { useI18n } from "../../i18n";
import { cn } from "../../lib/utils";
import { cardSurface } from "../../components/ui/card";
import { ErrorNotice } from "../../components/ui/data";
import { bookTextFont } from "./bookText";
import { ParagraphActions } from "./ParagraphActions";
import { ParagraphMenu, type ParagraphMenuAnchor } from "./ParagraphMenu";
import { ParagraphEditor } from "./ParagraphEditor";
import { useChapterWindows } from "./useChapterWindows";
import { useReaderSelection } from "./useReaderSelection";

const ParagraphRow = memo(function ParagraphRow({
  segment, sourceOnly, sourceLanguage, targetLanguage, expanded, onMenu,
}: {
  segment: ReaderSegment;
  sourceOnly: boolean;
  sourceLanguage: string;
  targetLanguage?: string;
  expanded: boolean;
  onMenu: (anchor: ParagraphMenuAnchor) => void;
}) {
  return (
    <div
      id={`paragraph-${segment.index}`}
      tabIndex={-1}
      className={cn("grid", sourceOnly ? "" : "grid-cols-2", "border-b border-subtle")}
    >
      <div className="min-w-0 border-r border-subtle p-4 text-sm">
        <p data-reader-text="source" lang={sourceLanguage}
          className={cn("whitespace-pre-wrap leading-relaxed [overflow-wrap:anywhere]", bookTextFont(segment.source))}>
          {segment.source}
        </p>
      </div>
      {!sourceOnly && (
        <ParagraphActions index={segment.index} target={segment.target} targetLanguage={targetLanguage}
          expanded={expanded} onMenu={onMenu} />
      )}
    </div>
  );
});

export function VirtualChapter({
  pid, ci, initial, sourceOnly, busy, readOnly, sourceLanguage, targetLanguage,
}: {
  pid: string;
  ci: number;
  initial: ChapterWindow;
  sourceOnly: boolean;
  busy: boolean;
  readOnly: boolean;
  sourceLanguage: string;
  targetLanguage?: string;
}) {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const container = useRef<HTMLDivElement>(null);
  const [scroller, setScroller] = useState<HTMLElement | null>(null);
  const [margin, setMargin] = useState(0);
  const [menu, setMenu] = useState<
    (ParagraphMenuAnchor & { selectionText: () => Promise<string> }) | null
  >(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  const [editor, setEditor] = useState<{
    segment: ReaderSegment; view: "edit" | "history" | "precision";
  } | null>(null);
  const selection = useReaderSelection(container, pid, ci, sourceOnly,
    t("proofreading.waitingForTranslation"), t("review.emptyTranslation"), t("proofreading.copyFailed"));
  const openMenu = useCallback((anchor: ParagraphMenuAnchor) => {
    setMenu({ ...anchor, selectionText: selection.captureText() });
  }, [selection.captureText]);
  const pinKey = selection.pins.join(",");
  const virtualizer = useVirtualizer({
    count: initial.total,
    getScrollElement: () => scroller,
    estimateSize: () => 160,
    overscan: 6,
    scrollMargin: margin,
    rangeExtractor: useCallback((range: Parameters<typeof defaultRangeExtractor>[0]) =>
      [...new Set([...defaultRangeExtractor(range), ...selection.pins])].sort((a, b) => a - b),
    // Only selection endpoints stay mounted as the viewport moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pinKey]),
  });
  const virtualRows = virtualizer.getVirtualItems();
  const positions = virtualRows.map((r) => r.index);
  if (editor) positions.push(editor.segment.position);
  const reader = useChapterWindows(pid, ci, sourceOnly, busy, initial, positions, editor?.segment);
  const active = editor && (reader.byIndex.get(editor.segment.index) || editor.segment);
  const menuSegment = menu && reader.byIndex.get(menu.index);
  const openEditor = useCallback((index: number, view: "edit" | "history" | "precision") => {
    const segment = reader.byIndex.get(index);
    if (segment) setEditor({ segment, view });
  }, [reader.byIndex]);
  useLayoutEffect(() => {
    const element = container.current!;
    const scroll = element.closest<HTMLElement>('[data-slot="content"]')!;
    setScroller(scroll);
    const measure = () => setMargin(
      element.getBoundingClientRect().top - scroll.getBoundingClientRect().top + scroll.scrollTop,
    );
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroll);
    window.addEventListener("resize", measure);
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, []);
  const requested = params.get("segment");
  const focused = useRef<string | null>(null);
  useEffect(() => {
    if (initial.position == null || !requested || !scroller || !margin) return;
    if (focused.current === requested) return;
    virtualizer.scrollToIndex(initial.position, { align: "center" });
    const element = document.getElementById(`paragraph-${Number(requested)}`);
    if (element) {
      element.focus({ preventScroll: true });
      focused.current = requested;
    }
  }, [requested, initial.position, scroller, margin, virtualRows, virtualizer]);
  return (
    <>
      <ErrorNotice error={reader.error} />
      <div ref={container} className={cn("relative", cardSurface)} data-testid="chapter-reader"
        style={{ height: virtualizer.getTotalSize() }}>
        {virtualRows.map((row) => {
          const segment = reader.rows.get(row.index);
          return (
            <div key={row.key} data-index={row.index} data-paragraph-position={row.index}
              ref={virtualizer.measureElement} className="absolute left-0 top-0 w-full"
              style={{ transform: `translateY(${row.start - margin}px)` }}>
              {segment ? (
                <ParagraphRow segment={segment} sourceOnly={sourceOnly}
                  sourceLanguage={sourceLanguage} targetLanguage={targetLanguage}
                  expanded={menu?.index === segment.index} onMenu={openMenu} />
              ) : (
                <div aria-busy="true" className="border-b border-subtle h-40 p-4 text-sm text-muted-foreground">
                  {t("progress.loading")}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {!sourceOnly && menu && menuSegment && (
        <ParagraphMenu anchor={menu} source={menuSegment.source} target={menuSegment.target}
          disabled={readOnly || !!reader.error} onOpen={openEditor} onClose={closeMenu}
          selectionText={menu.selectionText} />
      )}
      {!sourceOnly && editor && active && (
        <ParagraphEditor key={active.index} pid={pid} chapterIndex={ci} segment={active}
          initialView={editor.view} readOnly={readOnly || !!reader.error} busy={busy}
          sourceLanguage={sourceLanguage} targetLanguage={targetLanguage} onClose={() => setEditor(null)}
          onRefresh={async () => { await reader.refresh(); }} />
      )}
    </>
  );
}
