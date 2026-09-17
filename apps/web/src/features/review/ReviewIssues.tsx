import { useState } from "react";
import { useI18n } from "@/i18n";
import type { ReviewItem } from "@/lib/api";
import { Input, Label, Select } from "@/components/ui/form";
import { Badge } from "@/components/ui/badge";
import { ReviewItemDetails } from "./ReviewItemDetails";
import { itemStatuses, itemType } from "./reviewData";

export function ReviewIssues({
  pid,
  items,
}: {
  pid: string;
  items: ReviewItem[];
}) {
  const { t } = useI18n();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const filtered = items.filter(
    (item) =>
      (status === "all" || item.status === status) &&
      JSON.stringify(item)
        .toLocaleLowerCase()
        .includes(search.trim().toLocaleLowerCase()),
  );
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
        {Object.entries(itemStatuses).map(([value, { key }]) => {
          const count = items.filter(
            (item) => (item.status || "pending") === value,
          ).length;
          return (
            count > 0 && (
              <span key={value} className="text-muted-foreground">
                {t(key)}{" "}
                <strong className="ml-1 font-medium tabular-nums text-foreground">
                  {count}
                </strong>
              </span>
            )
          );
        })}
      </div>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_13rem]">
        <div className="space-y-2">
          <Label htmlFor="issue-search">{t("review.searchIssues")}</Label>
          <Input
            id="issue-search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="issue-status">{t("review.filterStatus")}</Label>
          <Select
            id="issue-status"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="all">{t("review.allItems")}</option>
            {Object.entries(itemStatuses).map(([value, { key }]) => (
              <option key={value} value={value}>
                {t(key)}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <ul aria-label={t("common.reviewIssues")} className="divide-y border-y">
        {filtered.map((item) => {
          const state = itemStatuses[item.status || "pending"];
          return (
            <li key={item.id} className="py-4 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs text-muted-foreground">
                  {item.location
                    ? t("review.paragraph", {
                        title: item.location.chapter_title,
                        number: item.location.text_index + 1,
                      })
                    : t("review.locationUnknown")}{" "}
                  · {t(itemType(item))}
                </span>
                <Badge variant={state.tone}>{t(state.key)}</Badge>
              </div>
              <p className="text-sm whitespace-pre-wrap [overflow-wrap:anywhere]">
                {item.detail || t(itemType(item))}
              </p>
              <details>
                <summary className="w-fit cursor-pointer text-sm text-muted-foreground">
                  {t("review.issueDetails")}
                </summary>
                <ReviewItemDetails pid={pid} item={item} />
              </details>
            </li>
          );
        })}
      </ul>
      {!filtered.length && (
        <p className="text-sm text-muted-foreground">{t("list.noMatches")}</p>
      )}
    </div>
  );
}
