import { useState } from "react";
import { useI18n } from "../../i18n";
import type { ReviewItem } from "../../api/types";
import { Input, Label } from "../../components/ui/form";
import { Select, SelectItem } from "../../components/ui/select";
import { Badge } from "../../components/ui/badge";
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
  // Search the fields the user can actually see, not the JSON serialization: key
  // names, enum values and issue-id hashes made every query a near-full match
  // (audit I-2).
  const searchable = (item: ReviewItem) =>
    [
      item.detail,
      item.suggestion,
      item.location?.chapter_title,
      item.location?.source,
      item.location?.current_target,
      ...Object.values(item.issue || {}).filter(
        (value): value is string => typeof value === "string",
      ),
      ...(item.changes || []).flatMap((change) =>
        Object.values(change).filter((value): value is string => typeof value === "string"),
      ),
    ]
      .join(" ")
      .toLocaleLowerCase();
  const filtered = items.filter(
    (item) =>
      (status === "all" || item.status === status) &&
      searchable(item).includes(search.trim().toLocaleLowerCase()),
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
      <div className="grid grid-cols-[minmax(0,1fr)_208px] gap-3">
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
            onValueChange={setStatus}
          >
            <SelectItem value="all">{t("review.allItems")}</SelectItem>
            {Object.entries(itemStatuses).map(([value, { key }]) => (
              <SelectItem key={value} value={value}>
                {t(key)}
              </SelectItem>
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
