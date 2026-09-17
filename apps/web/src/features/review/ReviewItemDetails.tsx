import { Link } from "react-router-dom";
import { useI18n } from "@/i18n";
import type { ReviewItem, ReviewLocation } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { StructuredData } from "@/components/ui/data";
import { text } from "./reviewData";

export function ReviewItemDetails({
  pid,
  item,
}: {
  pid: string;
  item: ReviewItem;
}) {
  const { t } = useI18n();
  const supporting = (item.evidence || []).filter(
    (location) =>
      location.chapter !== item.location?.chapter ||
      location.segment_index !== item.location?.segment_index,
  );
  return (
    <div className="mt-4 space-y-5 text-sm">
      {item.location && (
        <section className="space-y-2">
          <h4 className="font-medium">{t("review.currentTexts")}</h4>
          <Passage location={item.location} />
          <Link
            className="inline-block underline underline-offset-4"
            to={`/projects/${pid}/proofreading/${item.location.chapter}?segment=${item.location.segment_index}`}
          >
            {t("review.openProofreading")}
          </Link>
        </section>
      )}
      {item.suggestion && (
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
          {item.suggestion}
        </p>
      )}
      {!!item.changes?.length && (
        <section className="space-y-2">
          <h4 className="font-medium">{t("review.suggestedTranslation")}</h4>
          <p className="text-xs text-muted-foreground">
            {t("review.suggestionOnly")}
          </p>
          {item.changes.map((change, index) => (
            <p
              key={index}
              className="rounded-md bg-muted/40 p-3 whitespace-pre-wrap [overflow-wrap:anywhere]"
            >
              {text(change.suggested_target) || t("review.emptyTranslation")}
            </p>
          ))}
        </section>
      )}
      {!!item.publications?.length && (
        <section className="space-y-3">
          <h4 className="font-medium">{t("review.publication")}</h4>
          {item.publications.map((record, index) => {
            const applied = record.status === "applied";
            const failed =
              record.status === "failed" || record.status === "not_applied";
            const publication =
              record.publication && typeof record.publication === "object"
                ? (record.publication as Record<string, unknown>)
                : {};
            const published = applied && publication.status === "applied";
            const before = published ? publication.before : record.before;
            const after = published ? publication.target : record.after;
            const reason = publication.reason || record.reason;
            return (
              <div key={index} className="space-y-3 rounded-md border p-3">
                <Badge
                  variant={
                    applied ? "success" : failed ? "destructive" : "secondary"
                  }
                >
                  {t(
                    applied
                      ? "review.fixed"
                      : failed
                        ? "review.failed"
                        : record.status === "not_applied_no_net_change"
                          ? "review.unchanged"
                          : "review.pending",
                  )}
                </Badge>
                {failed && (
                  <p className="text-muted-foreground">
                    {t(
                      reason === "formal_target_changed"
                        ? "review.targetChanged"
                        : "review.publicationFailed",
                    )}
                  </p>
                )}
                <div className="grid gap-4 md:grid-cols-2">
                  {typeof before === "string" && (
                    <TextBlock
                      label={t("review.beforeRevision")}
                      value={before}
                    />
                  )}
                  {typeof after === "string" && (
                    <TextBlock
                      label={t(
                        published
                          ? "review.writtenTranslation"
                          : applied
                            ? "review.revisionText"
                            : "review.proposedText",
                      )}
                      value={after}
                    />
                  )}
                </div>
              </div>
            );
          })}
        </section>
      )}
      {!!supporting.length && (
        <section className="space-y-3">
          <h4 className="font-medium">{t("review.supportingPassages")}</h4>
          {supporting.map((location) => (
            <div
              key={`${location.chapter}:${location.segment_index}`}
              className="space-y-2"
            >
              <p className="text-xs text-muted-foreground">
                {t("review.paragraph", {
                  title: location.chapter_title,
                  number: location.text_index + 1,
                })}
              </p>
              <Passage location={location} />
            </div>
          ))}
        </section>
      )}
      {item.issue?.evidence != null && (
        <StructuredData value={item.issue.evidence} />
      )}
    </div>
  );
}

function Passage({ location }: { location: ReviewLocation }) {
  const { t } = useI18n();
  return (
    <div className="grid gap-4 rounded-md bg-muted/40 p-3 md:grid-cols-2">
      <TextBlock label={t("common.source")} value={location.source} />
      <TextBlock
        label={t("common.translation")}
        value={location.current_target ?? t("common.pending")}
      />
    </div>
  );
}

function TextBlock({ label, value }: { label: string; value: string }) {
  const { t } = useI18n();
  return (
    <div className="min-w-0 space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="whitespace-pre-wrap leading-relaxed [overflow-wrap:anywhere]">
        {value || t("review.emptyTranslation")}
      </p>
    </div>
  );
}
