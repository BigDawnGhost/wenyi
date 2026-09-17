import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { toast } from "sonner";
import { useI18n } from "@/i18n";
import { api, type ChapterSegments } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { RevisionHistory } from "./RevisionHistory";

export function ParagraphEditor({
  pid,
  chapterIndex,
  segment,
  initialView,
  readOnly,
  onClose,
}: {
  pid: string;
  chapterIndex: number;
  segment: ChapterSegments["segments"][number];
  initialView: "edit" | "history";
  readOnly: boolean;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const [view, setView] = useState(initialView);
  const [baseline, setBaseline] = useState(segment.target);
  const [draft, setDraft] = useState(segment.target ?? "");
  const history = useQuery({
    queryKey: ["segmentHistory", pid, chapterIndex, segment.index],
    queryFn: () => api.segmentHistory(pid, chapterIndex, segment.index),
    enabled: view === "history",
    refetchInterval: view === "history" ? 3000 : false,
  });
  const refresh = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["review", pid, chapterIndex] }),
      qc.invalidateQueries({ queryKey: ["chapters", pid] }),
      qc.invalidateQueries({
        queryKey: ["segmentHistory", pid, chapterIndex, segment.index],
      }),
    ]);
  const save = useMutation({
    mutationFn: () =>
      api.editSegment(
        pid,
        chapterIndex,
        segment.index,
        draft,
        baseline ?? null,
      ),
    onSuccess: async () => {
      await refresh();
      toast.success(t("review.translationSaved"));
      onClose();
    },
    onError: () => {
      void refresh();
    },
  });
  const stale = segment.target !== baseline;
  const disabled = readOnly || segment.target == null || save.isPending;
  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement;
    element.showModal();
    textarea.current?.focus({ preventScroll: true });
    return () => {
      element.close();
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus({ preventScroll: true });
    };
  }, []);
  useLayoutEffect(() => {
    const element = textarea.current;
    if (!element) return;
    const fit = () => {
      element.style.height = "0px";
      element.style.height = `${element.scrollHeight + 2}px`;
    };
    fit();
    let width = element.clientWidth;
    const observer = new ResizeObserver(() => {
      if (element.clientWidth !== width) {
        width = element.clientWidth;
        fit();
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [draft, view]);
  const close = () => {
    if (!save.isPending) onClose();
  };
  return createPortal(
    <dialog
      ref={dialog}
      aria-labelledby="paragraph-editor-title"
      aria-describedby="paragraph-editor-help"
      className="fixed inset-0 m-auto w-[calc(100%-2rem)] max-w-6xl max-h-[90dvh] overflow-hidden rounded-xl border bg-background p-0 text-foreground shadow-xl backdrop:bg-black/30 open:flex open:flex-col"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (
          event.clientX < rect.left ||
          event.clientX > rect.right ||
          event.clientY < rect.top ||
          event.clientY > rect.bottom
        )
          close();
      }}
    >
      <header className="shrink-0 space-y-2 border-b p-5 pr-14">
        <h2 id="paragraph-editor-title" className="text-lg font-medium">
          {t("proofreading.paragraphEditor")}
        </h2>
        <p id="paragraph-editor-help" className="text-sm text-muted-foreground">
          {t("proofreading.editorHelp")}
        </p>
        <button
          type="button"
          aria-label={t("common.close")}
          disabled={save.isPending}
          className="absolute right-4 top-5 rounded p-1 text-muted-foreground hover:bg-accent focus-visible:ring-1 focus-visible:ring-ring"
          onClick={close}
        >
          <X className="h-4 w-4" />
        </button>
      </header>
      <div
        role="tablist"
        aria-label={t("proofreading.paragraphEditor")}
        className="flex shrink-0 gap-5 border-b px-5"
      >
        {(["edit", "history"] as const).map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            id={`paragraph-tab-${tab}`}
            aria-controls="paragraph-panel"
            aria-selected={view === tab}
            tabIndex={view === tab ? 0 : -1}
            className={`border-b-2 py-3 text-sm ${view === tab ? "border-foreground font-medium" : "border-transparent text-muted-foreground"}`}
            onClick={() => setView(tab)}
            onKeyDown={(event) => {
              if (
                ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              ) {
                event.preventDefault();
                const next =
                  event.key === "Home"
                    ? "edit"
                    : event.key === "End"
                      ? "history"
                      : view === "edit"
                        ? "history"
                        : "edit";
                setView(next);
                document.getElementById(`paragraph-tab-${next}`)?.focus();
              }
            }}
          >
            {tab === "edit"
              ? t("common.translation")
              : t("proofreading.changeHistory")}
          </button>
        ))}
      </div>
      <div
        ref={body}
        className="min-h-0 overflow-y-auto overscroll-contain p-5"
        role="tabpanel"
        id="paragraph-panel"
        aria-labelledby={`paragraph-tab-${view}`}
      >
        <ErrorNotice
          error={save.error || (view === "history" ? history.error : undefined)}
        />
        {readOnly && (
          <p role="status" className="mb-4 text-sm text-muted-foreground">
            {t("proofreading.pauseToEdit")}
          </p>
        )}
        {stale && (
          <div
            role="status"
            className="mb-4 space-y-2 rounded-lg border bg-muted/50 p-3 text-sm"
          >
            <p>{t("proofreading.changedWhileEditing")}</p>
            <Button
              variant="outline"
              size="sm"
              disabled={save.isPending}
              onClick={() => {
                setBaseline(segment.target);
                setDraft(segment.target ?? "");
                save.reset();
              }}
            >
              {t("proofreading.loadLatest")}
            </Button>
          </div>
        )}
        <div className="grid gap-6 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <section className="min-w-0 space-y-3">
            <h3 className="text-sm font-medium">{t("common.source")}</h3>
            <p className="whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]">
              {segment.source}
            </p>
          </section>
          <section className="min-w-0 space-y-3">
            {view === "edit" ? (
              <>
                <label
                  htmlFor="paragraph-draft"
                  className="block text-sm font-medium"
                >
                  {t("review.editTranslation")}
                </label>
                <Textarea
                  ref={textarea}
                  id="paragraph-draft"
                  value={draft}
                  disabled={disabled}
                  className="min-h-44 resize-none overflow-hidden text-sm leading-relaxed"
                  onChange={(event) => setDraft(event.target.value)}
                />
              </>
            ) : history.isPending ? (
              <p className="text-sm text-muted-foreground">
                {t("progress.loading")}
              </p>
            ) : (
              <RevisionHistory
                entries={history.data || []}
                disabled={disabled}
                onUse={(value) => {
                  setDraft(value);
                  setView("edit");
                  save.reset();
                  body.current?.scrollTo({ top: 0 });
                }}
              />
            )}
          </section>
        </div>
      </div>
      <footer className="flex shrink-0 flex-wrap items-center justify-end gap-3 border-t bg-background p-4">
        {draft !== (baseline ?? "") && (
          <p className="mr-auto text-xs text-muted-foreground">
            {t("proofreading.unsavedChanges")}
          </p>
        )}
        <Button variant="outline" disabled={save.isPending} onClick={close}>
          {t("common.close")}
        </Button>
        {view === "edit" && (
          <Button
            disabled={disabled || stale || draft === (baseline ?? "")}
            onClick={() => save.mutate()}
          >
            {save.isPending ? t("common.saving") : t("review.saveTranslation")}
          </Button>
        )}
      </footer>
    </dialog>,
    document.body,
  );
}
