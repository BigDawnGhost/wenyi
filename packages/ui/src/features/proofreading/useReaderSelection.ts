import { useEffect, useState, type RefObject } from "react";
import { api } from "../../api/client";
import { copyText } from "./clipboard";
import { toast } from "../../lib/toast";

type Point = { position: number; side: "source" | "target"; offset: number };
type SelectionBounds = { start: Point; end: Point };

function point(node: Node, offset: number): Point | null {
  const element = node instanceof Element ? node : node.parentElement;
  const text = element?.closest<HTMLElement>("[data-reader-text]");
  const row = text?.closest<HTMLElement>("[data-paragraph-position]");
  if (!text || !row) return null;
  const range = document.createRange();
  range.selectNodeContents(text);
  range.setEnd(node, offset);
  return {
    position: Number(row.dataset.paragraphPosition),
    side: text.dataset.readerText as Point["side"],
    offset: range.toString().length,
  };
}

/** Pin selection endpoints, not every row between them. Copy fills any unloaded gap. */
export function useReaderSelection(
  container: RefObject<HTMLDivElement | null>,
  pid: string,
  ci: number,
  sourceOnly: boolean,
  waiting: string,
  empty: string,
  failed: string,
) {
  const [bounds, setBounds] = useState<SelectionBounds | null>(null);
  useEffect(() => {
    const changed = () => {
      const selection = window.getSelection();
      if (!selection?.rangeCount || selection.isCollapsed) {
        setBounds(null);
        return;
      }
      const range = selection.getRangeAt(0);
      if (!container.current?.contains(range.commonAncestorContainer)) {
        setBounds(null);
        return;
      }
      const start = point(range.startContainer, range.startOffset);
      const end = point(range.endContainer, range.endOffset);
      setBounds(start && end ? { start, end } : null);
    };
    document.addEventListener("selectionchange", changed);
    return () => document.removeEventListener("selectionchange", changed);
  }, [container]);
  const text = async () => {
    if (!bounds) return window.getSelection()?.toString() || "";
    const { start, end } = bounds;
    const parts: string[] = [];
    for (let offset = start.position; offset <= end.position; offset += 128) {
      const window = await api.chapterWindow(pid, ci, offset, Math.min(128, end.position - offset + 1));
      for (const segment of window.segments) {
        const values: ["source" | "target", string][] = [
          ["source", segment.source],
          ...(!sourceOnly
            ? [["target", segment.target == null ? waiting : segment.target || empty] as ["target", string]]
            : []),
        ];
        for (const [side, value] of values) {
          if (segment.position === start.position && start.side === "target" && side === "source") continue;
          if (segment.position === end.position && end.side === "source" && side === "target") continue;
          const from = segment.position === start.position && side === start.side ? start.offset : 0;
          const to = segment.position === end.position && side === end.side ? end.offset : value.length;
          parts.push(value.slice(from, to));
        }
      }
    }
    return parts.join("\n");
  };
  useEffect(() => {
    const copy = (event: ClipboardEvent) => {
      if (!bounds || document.querySelector('[role="dialog"]')) return;
      const mounted = container.current?.querySelectorAll("[data-paragraph-position]");
      const positions = new Set(Array.from(mounted || [], (el) =>
        Number((el as HTMLElement).dataset.paragraphPosition)));
      // Native copying is preferable when the selection has no virtual gaps.
      let complete = true;
      for (let position = bounds.start.position; position <= bounds.end.position; position++) {
        if (!positions.has(position)) { complete = false; break; }
      }
      if (complete) return;
      event.preventDefault();
      void text().then(copyText).catch(() => toast.error(failed));
    };
    document.addEventListener("copy", copy);
    return () => document.removeEventListener("copy", copy);
  });
  return { pins: bounds ? [bounds.start.position, bounds.end.position] : [], text };
}
