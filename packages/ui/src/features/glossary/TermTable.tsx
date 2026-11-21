import type { Term } from "../../api/types";
import { useI18n } from "../../i18n";
import { Button } from "../../components/ui/button";
import { Card, CardContent } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { EmptyState } from "../../components/ui/misc";
import { Eye, Pencil, Trash2 } from "lucide-react";
import { PAGE_SIZE, termTypes } from "./termTypes";

export function TermPagination({
  page,
  total,
  disabled,
  onChange,
}: {
  page: number;
  total: number;
  disabled?: boolean;
  onChange: (page: number) => void;
}) {
  const { t } = useI18n();
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return (
    <nav
      aria-label={t("glossary.paginationLabel")}
      className="flex flex-wrap items-center justify-end gap-3"
    >
      <span role="status" className="text-sm text-muted-foreground">
        {disabled
          ? t("progress.loading")
          : t("glossary.pagination", { page: page + 1, pages, count: total })}
      </span>
      <Button
        variant="outline"
        disabled={disabled || page === 0}
        onClick={() => onChange(page - 1)}
      >
        {t("glossary.previousPage")}
      </Button>
      <Button
        variant="outline"
        disabled={disabled || page + 1 >= pages}
        onClick={() => onChange(page + 1)}
      >
        {t("glossary.nextPage")}
      </Button>
    </nav>
  );
}

export function TermTable({
  terms,
  selected,
  loading,
  readOnly,
  searchQuery,
  type,
  onToggleSelect,
  onToggleSelectAll,
  onEdit,
  onDelete,
}: {
  terms: Term[] | undefined;
  selected: Set<string>;
  loading: boolean;
  readOnly: boolean;
  searchQuery: string;
  type: string;
  onToggleSelect: (source: string) => void;
  onToggleSelectAll: () => void;
  onEdit: (term: Term) => void;
  onDelete: (source: string) => void;
}) {
  const { t: tr } = useI18n();
  return (
    <Card>
      <CardContent className="overflow-x-auto p-0">
        <table className="w-full min-w-160 table-fixed text-sm">
          <colgroup>
            <col className="w-10" />
            <col />
            <col />
            <col className="w-36" />
            <col className="w-24" />
          </colgroup>
          <thead className="whitespace-nowrap border-b border-subtle text-xs tracking-wide text-muted-foreground">
            <tr>
              <th className="text-left px-4 py-3.5 font-medium">
                <input
                  type="checkbox"
                  aria-label={tr("glossary.selectPage")}
                  disabled={loading || !terms?.length}
                  checked={
                    terms
                      ? terms.length > 0 && terms.every((term) => selected.has(term.source))
                      : false
                  }
                  onChange={onToggleSelectAll}
                />
              </th>
              <th className="text-left px-4 py-3.5 font-medium">
                {tr("glossary.sourceTerm")}
              </th>
              <th className="text-left px-4 py-3.5 font-medium">
                {tr("glossary.translatedTerm")}
              </th>
              <th className="text-left px-4 py-3.5 font-medium">
                {tr("common.type")}
              </th>
              <th className="text-right px-4 py-3.5 font-medium">
                {tr("common.actions")}
              </th>
            </tr>
          </thead>
          <tbody>
            {(terms || []).map((t: Term) => (
              <tr
                key={t.source}
                className="border-b border-subtle last:border-0 transition-colors hover:bg-hover"
              >
                <td className="px-4 py-3.5">
                  <input
                    type="checkbox"
                    checked={selected.has(t.source)}
                    onChange={() => onToggleSelect(t.source)}
                  />
                </td>
                <td className="px-4 py-3.5 font-medium">
                  <span className="block truncate" title={t.source}>
                    {t.source}
                  </span>
                </td>
                <td className="px-4 py-3.5">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="block truncate" title={t.target}>
                      {t.target}
                    </span>
                    {t.status === "context_dependent" && (
                      <Badge variant="secondary" className="shrink-0">
                        {tr("glossary.contextDependent")}
                      </Badge>
                    )}
                    {t.status === "conflict" && (
                      <Badge variant="warning" className="shrink-0">
                        {tr("glossary.conflictBadge")}
                      </Badge>
                    )}
                  </div>
                </td>
                <td className="px-4 py-3.5">
                  <Badge variant="outline">
                    {termTypes()[t.type || ""] || t.type}
                  </Badge>
                </td>
                <td className="px-4 py-3.5 text-right">
                  <div className="flex justify-end gap-1">
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={tr(readOnly ? "glossary.termDetails" : "glossary.editTerm")}
                      onClick={() => onEdit(t)}
                    >
                      {readOnly
                        ? <Eye className="h-3.5 w-3.5" />
                        : <Pencil className="h-3.5 w-3.5" />}
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      disabled={readOnly}
                      aria-label={tr("glossary.deleteTerm")}
                      onClick={() => onDelete(t.source)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
            {terms && terms.length === 0 && (
              <tr>
                <td colSpan={5}>
                  <EmptyState>
                    {tr(
                      searchQuery || type
                        ? "glossary.noMatchingTerms"
                        : "glossary.noTermsYetTermsAreExtractedDuring",
                    )}
                  </EmptyState>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
