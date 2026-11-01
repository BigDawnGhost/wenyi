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
  sourceOnly = false,
  empty,
  onEdit,
}: {
  pid: string;
  entries: ContentsEntry[];
  readOnly: boolean;
  sourceOnly?: boolean;
  empty?: string;
  onEdit: (id: string) => void;
}) {
  const { t } = useI18n();
  return (
    <Card>
      <CardContent className="p-0">
        <div
          aria-hidden="true"
          className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_112px] gap-4 border-b px-4 py-3 text-xs text-muted-foreground"
        >
          <span>{t("contents.sourceTitle")}</span>
          <span>{t("contents.translatedTitle")}</span>
          <span className="text-right">{t("common.actions")}</span>
        </div>
        <ul aria-label={t("contents.title")} className="divide-y">
          {entries.map((entry) => (
            <li
              key={entry.id}
              className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_112px] items-start gap-4 p-4 text-sm"
            >
              <div
                className="min-w-0 [overflow-wrap:anywhere]"
                style={{
                  paddingInlineStart: `${Math.min(Math.max(entry.depth, 0), 6) * 12}px`,
                }}
              >
                <p>{entry.title || t("common.untitledChapter")}</p>
              </div>
              <div className="min-w-0 [overflow-wrap:anywhere]">
                <p
                  className={
                    entry.title_translated ? undefined : "text-muted-foreground"
                  }
                >
                  {entry.title_translated || t("contents.notTranslated")}
                </p>
              </div>
              <div className="flex justify-end gap-1">
                {entry.chapter_index !== null && (
                  <Link
                    to={`/projects/${pid}/proofreading/${entry.chapter_index}${entry.segment_index !== null ? `?segment=${entry.segment_index}` : ""}`}
                    aria-label={t(
                      sourceOnly ? "contents.readSource" : "contents.openChapter",
                    )}
                    title={t(
                      sourceOnly ? "contents.readSource" : "contents.openChapter",
                    )}
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
            {empty ?? t("contents.noEntries")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
