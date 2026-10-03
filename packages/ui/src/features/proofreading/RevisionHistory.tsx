import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import type { SegmentRevision } from "@/lib/api";

export function RevisionHistory({
  entries,
  disabled,
  onUse,
}: {
  entries: SegmentRevision[];
  disabled: boolean;
  onUse: (value: string) => void;
}) {
  const { t, locale } = useI18n();
  const labels = {
    translation: t("proofreading.initialTranslation"),
    polish: t("proofreading.polishing"),
    manual: t("proofreading.manualEdit"),
    update: t("proofreading.translationUpdate"),
    snapshot: t("proofreading.savedVersion"),
    before_polish: t("review.translationBeforePolishing"),
  };
  if (!entries.length)
    return (
      <p className="text-sm text-muted-foreground">
        {t("proofreading.noHistory")}
      </p>
    );
  return (
    <ol className="space-y-3" aria-label={t("proofreading.changeHistory")}>
      {entries.map((entry, i) => (
        <li key={entry.id}>
          <details open={i === 0} className="rounded-lg border">
            <summary className="cursor-pointer px-4 py-3 text-sm">
              <span className="font-medium">{labels[entry.kind]}</span>
              <span className="ml-3 text-xs text-muted-foreground">
                {entry.created_at
                  ? new Date(entry.created_at).toLocaleString(locale)
                  : t("proofreading.timeNotRecorded")}
              </span>
            </summary>
            <div className="space-y-4 border-t p-4">
              <div className="space-y-5">
                {entry.before != null && (
                  <div className="space-y-2">
                    <h3 className="text-xs font-medium text-muted-foreground">
                      {t("proofreading.beforeChange")}
                    </h3>
                    <p className="whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]">
                      {entry.before || t("review.emptyTranslation")}
                    </p>
                  </div>
                )}
                <div className="space-y-2">
                  <h3 className="text-xs font-medium text-muted-foreground">
                    {t("proofreading.savedVersion")}
                  </h3>
                  <p className="whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]">
                    {entry.after == null
                      ? t("proofreading.waitingForTranslation")
                      : entry.after || t("review.emptyTranslation")}
                  </p>
                </div>
              </div>
              {entry.after != null && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={disabled}
                  onClick={() => onUse(entry.after!)}
                >
                  {t("proofreading.useVersion")}
                </Button>
              )}
            </div>
          </details>
        </li>
      ))}
    </ol>
  );
}
