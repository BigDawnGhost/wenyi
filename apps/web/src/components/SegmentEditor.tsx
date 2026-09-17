import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";

export function SegmentEditor({
  value,
  disabled,
  onSave,
}: {
  value: string;
  disabled: boolean;
  onSave: (target: string) => Promise<void>;
}) {
  const { t: tr } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const save = useMutation({
    mutationFn: () => onSave(draft),
    onSuccess: () => {
      setEditing(false);
      toast.success(tr("review.translationSaved"));
    },
  });
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);
  return (
    <div className="p-3 text-sm">
      <ErrorNotice error={save.error} />
      {editing ? (
        <div className="space-y-2">
          <Textarea
            aria-label={tr("review.editTranslation")}
            value={draft}
            disabled={disabled || save.isPending}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              disabled={disabled || save.isPending}
              onClick={() => save.mutate()}
            >
              {save.isPending
                ? tr("common.saving")
                : tr("review.saveTranslation")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={save.isPending}
              onClick={() => setEditing(false)}
            >
              {tr("common.cancel")}
            </Button>
          </div>
        </div>
      ) : (
        <button
          disabled={disabled}
          className="text-left w-full whitespace-pre-wrap min-h-10 rounded hover:bg-muted disabled:cursor-default"
          onClick={() => {
            setDraft(value);
            save.reset();
            setEditing(true);
          }}
        >
          {value || (
            <span className="text-muted-foreground">
              {tr(
                disabled
                  ? "review.emptyTranslation"
                  : "review.emptyTranslationClickToEdit",
              )}
            </span>
          )}
        </button>
      )}
    </div>
  );
}
