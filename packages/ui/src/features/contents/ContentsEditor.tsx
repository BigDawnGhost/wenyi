import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useI18n } from "../../i18n";
import { Button } from "../../components/ui/button";
import { Label, Textarea } from "../../components/ui/form";
import { ErrorNotice } from "../../components/ui/data";
import { Dialog } from "../../components/ui/misc";

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
  const draftField = useRef<HTMLTextAreaElement>(null);
  const [baseline, setBaseline] = useState(target);
  const [draft, setDraft] = useState(target ?? "");
  const save = useMutation({
    mutationFn: () => onSave(draft.trim(), baseline),
    onSuccess: onClose,
  });
  const stale = target !== baseline;
  return (
    <Dialog
      open
      onClose={onClose}
      title={t("contents.editTitle")}
      closeDisabled={save.isPending}
      initialFocus={draftField}
      className="max-w-2xl"
    >
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault();
          if (!readOnly && !stale && !save.isPending && draft.trim())
            save.mutate();
        }}
      >
        <ErrorNotice error={save.error} />
        {stale && (
          <div
            role="status"
            className="space-y-2 rounded-lg bg-surface p-3 text-sm"
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
            ref={draftField}
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
            {t("common.save")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
