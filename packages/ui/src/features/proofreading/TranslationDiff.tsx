import { useMemo, useState } from "react";
import { diffArrays, diffChars } from "diff";
import { useI18n } from "@/i18n";

type WordSegmenter = {
  segment(text: string): Iterable<{ segment: string }>;
};

const limits = { maxEditLength: 400, timeout: 40 };
const maxTextLength = 20_000;

function wordSegmenter(language?: string): WordSegmenter | undefined {
  // Use the project language only, never the browser's or interface's locale.
  if (!language) return undefined;
  const Segmenter = (
    Intl as typeof Intl & {
      Segmenter?: new (
        locale: string,
        options: { granularity: "word" },
      ) => WordSegmenter;
    }
  ).Segmenter;
  if (!Segmenter) return undefined;
  try {
    return new Segmenter(language === "zh" ? "zh-Hans" : language, {
      granularity: "word",
    });
  } catch {
    return undefined;
  }
}

function changes(before: string, after: string, language?: string) {
  if (before.length + after.length > maxTextLength) return undefined;
  const segmenter = wordSegmenter(language);
  // diffWords normalizes whitespace. Exact native segments plus diffArrays
  // retain every original space, newline and punctuation mark on both sides.
  if (segmenter) {
    const tokens = (text: string) =>
      Array.from(segmenter.segment(text), (part) => part.segment);
    return diffArrays(tokens(before), tokens(after), limits)?.map((part) => ({
      ...part,
      value: part.value.join(""),
    }));
  }
  // jsdiff 9 tokenizes characters by Unicode code point, not UTF-16 unit.
  return diffChars(before, after, limits);
}

/**
 * Read-only comparison. Mount this component only while its containing
 * revision/paragraph is expanded so collapsed items do not compute diffs.
 */
export function TranslationDiff({
  before,
  after,
  language,
}: {
  before: string;
  after: string;
  language?: string;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<"changes" | "full">("changes");
  const identical = before === after;
  const parts = useMemo(
    () => (mode === "changes" && !identical ? changes(before, after, language) : []),
    [before, after, language, mode, identical],
  );
  const tooLarge = mode === "changes" && !identical && parts === undefined;
  const displayedMode = tooLarge ? "full" : mode;
  const prose =
    "whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]";
  const removed = "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200";
  const added = "bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-200";

  return (
    <div className="space-y-3" data-testid="translation-diff">
      <div
        className="flex flex-wrap gap-2"
        role="group"
        aria-label={t("proofreading.diffChanges")}
      >
        {(["changes", "full"] as const).map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={displayedMode === value}
            disabled={value === "changes" && tooLarge}
            onClick={() => setMode(value)}
            className="rounded-md border px-3 py-1.5 text-sm aria-pressed:bg-muted disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
          >
            {t(
              value === "changes"
                ? "proofreading.diffChanges"
                : "proofreading.diffFullText",
            )}
          </button>
        ))}
      </div>
      {identical && (
        <p role="status" className="text-sm text-muted-foreground">
          {t("proofreading.diffUnchanged")}
        </p>
      )}
      {tooLarge && (
        <p role="status" className="text-sm text-muted-foreground">
          {t("proofreading.diffTooLarge")}
        </p>
      )}
      {displayedMode === "full" ? (
        <div className="grid gap-4 md:grid-cols-2">
          {([
            ["proofreading.beforeChange", before],
            ["proofreading.savedVersion", after],
          ] as const).map(([label, text]) => (
            <section key={label} className="min-w-0 space-y-2">
              <h3 className="text-xs font-medium text-muted-foreground">
                {t(label)}
              </h3>
              <p className={prose}>
                {text === "" ? t("review.emptyTranslation") : text}
              </p>
            </section>
          ))}
        </div>
      ) : (
        <>
          <div className="flex flex-wrap gap-3 text-xs">
            <span>
              <del className={removed}>{t("proofreading.diffRemoved")}</del>
            </span>
            <span>
              <ins className={added}>{t("proofreading.diffAdded")}</ins>
            </span>
          </div>
          <p className={prose} data-testid="translation-diff-text">
            {identical
              ? before === ""
                ? t("review.emptyTranslation")
                : before
              : parts?.map((part, index) =>
                  part.removed ? (
                    <del key={index} className={removed}>
                      {part.value}
                    </del>
                  ) : part.added ? (
                    <ins key={index} className={added}>
                      {part.value}
                    </ins>
                  ) : (
                    <span key={index}>{part.value}</span>
                  ),
                )}
          </p>
        </>
      )}
    </div>
  );
}
