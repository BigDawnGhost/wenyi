import { useState } from "react";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import type { SegmentRevision } from "@/lib/api";
import { TranslationDiff } from "./TranslationDiff";

export function RevisionHistory({
  entries,
  disabled,
  onUse,
  language,
  beforePolish,
  current,
}: {
  entries: SegmentRevision[];
  disabled: boolean;
  onUse: (value: string) => void;
  language?: string;
  beforePolish?: string | null;
  current?: string | null;
}) {
  const { t } = useI18n();
  const labels = {
    translation: t("proofreading.initialTranslation"),
    polish: t("proofreading.polishing"),
    manual: t("proofreading.manualEdit"),
    update: t("proofreading.translationUpdate"),
    snapshot: t("proofreading.savedVersion"),
    before_polish: t("review.translationBeforePolishing"),
  };
  // Older projects may retain only initial/current snapshots, not a polish revision.
  // Never describe that comparison as the result of polishing alone.
  const fallback =
    beforePolish != null &&
    current != null &&
    !entries.some(
      (entry) =>
        entry.kind === "polish" && entry.before != null && entry.after != null,
    )
      ? {
          id: "initial-current-comparison",
          kind: "snapshot" as const,
          before: beforePolish,
          after: current,
          created_at: null,
        }
      : undefined;
  const displayed = fallback ? [fallback, ...entries] : entries;
  if (!displayed.length)
    return (
      <p className="text-sm text-muted-foreground">
        {t("proofreading.noHistory")}
      </p>
    );
  return (
    <ol className="space-y-3" aria-label={t("proofreading.changeHistory")}>
      {displayed.map((entry, i) => (
        <HistoryEntry
          key={entry.id}
          entry={entry}
          label={
            entry === fallback
              ? t("proofreading.initialCurrentComparison")
              : labels[entry.kind]
          }
          notice={
            entry === fallback ? t("proofreading.initialCurrentNotice") : undefined
          }
          defaultOpen={i === 0}
          language={language}
          disabled={disabled}
          onUse={onUse}
        />
      ))}
    </ol>
  );
}

function HistoryEntry({
  entry,
  label,
  notice,
  defaultOpen,
  language,
  disabled,
  onUse,
}: {
  entry: SegmentRevision;
  label: string;
  notice?: string;
  defaultOpen: boolean;
  language?: string;
  disabled: boolean;
  onUse: (value: string) => void;
}) {
  const { t, locale } = useI18n();
  const [expanded, setExpanded] = useState(defaultOpen);
  return (
    <li>
      <details
        open={expanded}
        onToggle={(event) => setExpanded(event.currentTarget.open)}
        className="rounded-lg border"
      >
        <summary className="cursor-pointer px-4 py-3 text-sm">
          <span className="font-medium">{label}</span>
          <span className="ml-3 text-xs text-muted-foreground">
            {entry.created_at
              ? new Date(entry.created_at).toLocaleString(locale)
              : t("proofreading.timeNotRecorded")}
          </span>
        </summary>
        {expanded && (
          <div className="space-y-4 border-t p-4">
            {notice && (
              <p className="text-xs text-muted-foreground">{notice}</p>
            )}
            {entry.before != null && entry.after != null ? (
              <TranslationDiff
                before={entry.before}
                after={entry.after}
                language={language}
              />
            ) : (
              <div className="space-y-5">
                {entry.before != null && (
                  <section className="space-y-2">
                    <h3 className="text-xs font-medium text-muted-foreground">
                      {t("proofreading.beforeChange")}
                    </h3>
                    <p className="whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]">
                      {entry.before || t("review.emptyTranslation")}
                    </p>
                  </section>
                )}
                <section className="space-y-2">
                  <h3 className="text-xs font-medium text-muted-foreground">
                    {t("proofreading.savedVersion")}
                  </h3>
                  <p className="whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]">
                    {entry.after == null
                      ? t("proofreading.waitingForTranslation")
                      : entry.after || t("review.emptyTranslation")}
                  </p>
                </section>
              </div>
            )}
            {entry.after != null && !notice && (
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
        )}
      </details>
    </li>
  );
}
