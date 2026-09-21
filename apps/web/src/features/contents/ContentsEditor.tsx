import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation } from "@tanstack/react-query";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Label, Textarea } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";

export function ContentsEditor({
  source,
  target,
  readOnly,
  onSave,
  onClose,
}: {
  source: string;
  target: string | null;
  readOnly: boolean;
  onSave: (target: string, expected: string | null) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const [baseline, setBaseline] = useState(target);
  const [draft, setDraft] = useState(target ?? "");
  const save = useMutation({
    mutationFn: () => onSave(draft.trim(), baseline),
    onSuccess: onClose,
  });
  const stale = target !== baseline;
  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement;
    element.showModal();
    return () => {
      element.close();
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus({ preventScroll: true });
    };
  }, []);
  return createPortal(
    <dialog
      ref={dialog}
      aria-labelledby="contents-editor-title"
      className="fixed inset-0 m-auto max-h-[90dvh] w-[calc(100%-2rem)] max-w-2xl overflow-y-auto rounded-xl border bg-background p-5 text-foreground shadow-xl backdrop:bg-black/30"
      onCancel={(event) => {
        event.preventDefault();
        if (!save.isPending) onClose();
      }}
    >
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault();
          if (!readOnly && !stale && !save.isPending && draft.trim())
            save.mutate();
        }}
      >
        <h2 id="contents-editor-title" className="text-lg font-medium">
          {t("contents.editTitle")}
        </h2>
        <ErrorNotice error={save.error} />
        {readOnly && (
          <p role="status" className="text-sm text-muted-foreground">
            {t("contents.unavailable")}
          </p>
        )}
        {stale && (
          <div
            role="status"
            className="space-y-2 rounded-lg border bg-muted/50 p-3 text-sm"
          >
            <p>{t("contents.changedWhileEditing")}</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={save.isPending}
              onClick={() => {
                setBaseline(target);
                setDraft(target ?? "");
                save.reset();
              }}
            >
              {t("contents.loadLatest")}
            </Button>
          </div>
        )}
        <div className="space-y-2 text-sm">
          <p className="font-medium">{t("contents.sourceTitle")}</p>
          <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
            {source || t("common.untitledChapter")}
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="contents-title-draft">
            {t("contents.translatedTitle")}
          </Label>
          <Textarea
            id="contents-title-draft"
            autoFocus
            required
            rows={3}
            value={draft}
            disabled={readOnly || save.isPending}
            onChange={(event) => setDraft(event.target.value)}
          />
        </div>
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={save.isPending}
            onClick={onClose}
          >
            {t("common.cancel")}
          </Button>
          <Button
            type="submit"
            disabled={
              readOnly ||
              stale ||
              save.isPending ||
              !draft.trim() ||
              draft.trim() === (baseline ?? "")
            }
          >
            {save.isPending ? t("common.saving") : t("common.save")}
          </Button>
        </div>
      </form>
    </dialog>,
    document.body,
  );
}
