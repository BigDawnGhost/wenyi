import { useState } from "react";
import { useI18n } from "@/i18n";
import { Input, Label } from "@/components/ui/form";
import { StructuredData } from "@/components/ui/data";

export function ReviewIssues({
  issues,
  empty,
}: {
  issues: Record<string, unknown>[];
  empty: string;
}) {
  const { t } = useI18n();
  const [search, setSearch] = useState("");
  const filtered = issues.filter((issue) =>
    JSON.stringify(issue)
      .toLocaleLowerCase()
      .includes(search.trim().toLocaleLowerCase()),
  );
  if (!issues.length)
    return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <div className="space-y-3">
      <Label htmlFor="issue-search">{t("review.searchIssues")}</Label>
      <Input
        id="issue-search"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />
      <ul aria-label={t("common.reviewIssues")} className="divide-y">
        {filtered.map((issue, index) => (
          <li key={String(issue.issue_id ?? index)} className="py-3 space-y-2">
            <p className="text-sm whitespace-pre-wrap break-words">
              {String(
                issue.detail ??
                  issue.explanation ??
                  issue.type ??
                  t("common.reviewIssues"),
              )}
            </p>
            <details>
              <summary className="text-sm text-muted-foreground cursor-pointer">
                {t("review.issueDetails")}
              </summary>
              <div className="mt-3">
                <StructuredData value={issue} />
              </div>
            </details>
          </li>
        ))}
      </ul>
      {!filtered.length && (
        <p className="text-sm text-muted-foreground">{t("list.noMatches")}</p>
      )}
    </div>
  );
}
