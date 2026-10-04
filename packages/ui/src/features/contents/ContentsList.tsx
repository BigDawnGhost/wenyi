import { Link } from "react-router-dom";
import { BookOpen, Pencil } from "lucide-react";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

export interface ContentsEntry {
  id: string;
  title: string;
  title_translated: string | null;
  depth: number;
  chapter_index: number | null;
  segment_index: number | null;
}

export function ContentsList({
  pid,
  entries,
  readOnly,
  onEdit,
}: {
  pid: string;
  entries: ContentsEntry[];
  readOnly: boolean;
  onEdit: (id: string) => void;
}) {
  const { t } = useI18n();
  return (
    <Card>
      <CardContent className="p-0">
        <div
          aria-hidden="true"
          className="hidden grid-cols-[minmax(0,1fr)_minmax(0,1fr)_7rem] gap-4 border-b px-4 py-3 text-xs text-muted-foreground lg:grid"
        >
          <span>{t("contents.sourceTitle")}</span>
          <span>{t("contents.translatedTitle")}</span>
          <span className="text-right">{t("common.actions")}</span>
        </div>
        <ul aria-label={t("contents.title")} className="divide-y">
          {entries.map((entry) => (
            <li
              key={entry.id}
              className="relative grid grid-cols-1 items-start gap-x-4 gap-y-3 p-4 text-sm lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_7rem]"
            >
              <div
                className="min-w-0 [overflow-wrap:anywhere]"
                style={{
                  paddingInlineStart: `${Math.min(Math.max(entry.depth, 0), 6) * 12}px`,
                }}
              >
                <span className="mb-1 block min-h-8 pr-28 text-xs text-muted-foreground lg:hidden">
                  {t("contents.sourceTitle")}
                </span>
                <p>{entry.title || t("common.untitledChapter")}</p>
              </div>
              <div className="min-w-0 [overflow-wrap:anywhere]">
                <span className="mb-1 block text-xs text-muted-foreground lg:hidden">
                  {t("contents.translatedTitle")}
                </span>
                <p
                  className={
                    entry.title_translated ? undefined : "text-muted-foreground"
                  }
                >
                  {entry.title_translated || t("contents.notTranslated")}
                </p>
              </div>
              <div className="absolute right-4 top-4 flex justify-end gap-1 lg:static">
                {entry.chapter_index !== null && (
                  <Link
                    to={`/projects/${pid}/proofreading/${entry.chapter_index}${entry.segment_index !== null ? `?segment=${entry.segment_index}` : ""}`}
                    aria-label={t("contents.openChapter")}
                    title={t("contents.openChapter")}
                    className="inline-flex h-8 w-8 items-center justify-center rounded-md hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <BookOpen className="h-4 w-4" aria-hidden="true" />
                  </Link>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  aria-label={t("contents.editTitle")}
                  title={t("contents.editTitle")}
                  disabled={readOnly}
                  onClick={() => onEdit(entry.id)}
                >
                  <Pencil className="h-4 w-4" aria-hidden="true" />
                </Button>
              </div>
            </li>
          ))}
        </ul>
        {entries.length === 0 && (
          <p className="p-6 text-sm text-muted-foreground">
            {t("contents.noEntries")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
