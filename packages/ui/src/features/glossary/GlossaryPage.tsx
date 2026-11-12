import { useI18n } from "../../i18n";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "../../lib/toast";
import { errorMessage } from "../../lib/error-codes";
import { api, isProjectBusy } from "../../api/client";
import type { Term } from "../../api/types";
import { useProjectQuery } from "../../lib/projectQuery";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Input } from "../../components/ui/form";
import { Select, SelectDismissScope, SelectItem } from "../../components/ui/select";
import { ErrorNotice, ReadOnlyNotice } from "../../components/ui/data";
import { PAGE_SIZE, termTypes } from "./termTypes";
import { TermPagination, TermTable } from "./TermTable";
import { GlossaryActions } from "./GlossaryActions";
import { SelectionBar } from "./SelectionBar";
import { ConflictList } from "./ConflictList";
import { AddTermDialog } from "./dialogs/AddTermDialog";
import { TermDetailsDialog } from "./dialogs/TermDetailsDialog";
import { ImportDialog } from "./dialogs/ImportDialog";

export default function GlossaryPage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setSearchQuery(q.trim()), 250);
    return () => clearTimeout(timer);
  }, [q]);
  const requestedType = searchParams.get("type") || "";
  const type = Object.keys(termTypes()).includes(requestedType) ? requestedType : "";
  const filterKey = JSON.stringify([pid, searchQuery, type]);
  const [pagination, setPagination] = useState({ filterKey, page: 0 });
  const page = pagination.filterKey === filterKey ? pagination.page : 0;
  const changePage = (value: number) => {
    setSelected(new Set());
    setPagination({ filterKey, page: value });
  };
  const setType = (value: string) =>
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (value) next.set("type", value);
      else next.delete("type");
      return next;
    });
  const [addOpen, setAddOpen] = useState(false);
  const [editTerm, setEditTerm] = useState<Term | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [importOpen, setImportOpen] = useState(false);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const exportRef = useRef<HTMLDivElement>(null);

  const { data: project } = useProjectQuery(pid);
  const busy = isProjectBusy(project?.status);
  const showReading = project?.source_lang === "ja";
  const {
    data: termPage,
    error: termsError,
    isPending: termsLoading,
  } = useQuery({
    queryKey: ["terms", pid, searchQuery, type, page],
    queryFn: ({ signal }) =>
      api.listTermsPage(
        pid,
        {
          q: searchQuery || undefined,
          type: type || undefined,
          offset: page * PAGE_SIZE,
          limit: PAGE_SIZE,
        },
        signal,
      ),
    enabled: !!pid,
    gcTime: 60_000,
  });
  const terms = termPage?.items;
  useEffect(() => {
    setSelected(new Set());
    setPagination({ filterKey, page: 0 });
  }, [filterKey]);
  useEffect(() => {
    if (!termPage) return;
    const lastPage = Math.max(0, Math.ceil(termPage.total / PAGE_SIZE) - 1);
    if (page > lastPage) setPagination({ filterKey, page: lastPage });
  }, [termPage, page, filterKey]);
  const { data: conflicts, error: conflictsError } = useQuery({
    queryKey: ["conflicts", pid],
    queryFn: () => api.listConflicts(pid),
    enabled: !!pid,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["terms", pid] });
    qc.invalidateQueries({ queryKey: ["conflicts", pid] });
    setSelected(new Set());
  };
  const del = useMutation({
    mutationFn: (s: string) => api.deleteTerm(pid, s),
    onSuccess: () => {
      invalidate();
      toast.success(tr("glossary.deleted"));
    },
    onError: (e) => toast.error(errorMessage(e, tr)),
  });

  // Batch delete reports partial success instead of collapsing to one failure.
  // A refused term must not hide the terms that were deleted, and it stays
  // selected so the user can retry exactly the ones that failed.
  const batchDelete = useMutation({
    mutationFn: async () => {
      const ids = [...selected];
      const results = await Promise.allSettled(
        ids.map((source) => api.deleteTerm(pid, source)),
      );
      const deleted: string[] = [];
      const rejected: { source: string; reason: unknown }[] = [];
      results.forEach((result, index) => {
        if (result.status === "fulfilled") deleted.push(ids[index]);
        else rejected.push({ source: ids[index], reason: result.reason });
      });
      return { deleted, rejected };
    },
    onSuccess: ({ deleted, rejected }) => {
      qc.invalidateQueries({ queryKey: ["terms", pid] });
      qc.invalidateQueries({ queryKey: ["conflicts", pid] });
      setSelected(new Set(rejected.map(({ source }) => source)));
      if (rejected.length === 0) {
        toast.success(tr("glossary.selectedTermsDeleted"));
        return;
      }
      toast.error(
        tr("glossary.couldNotDeleteSelectedTermsPartially", {
          deleted: deleted.length,
          failed: rejected.length,
          error: errorMessage(rejected[0].reason, tr),
        }),
      );
    },
    onError: (e) => toast.error(errorMessage(e, tr)),
  });

  const toggleSelect = (source: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(source)) next.delete(source);
      else next.add(source);
      return next;
    });
  };
  const toggleSelectAll = () => {
    if (!terms) return;
    if (terms.every((term) => selected.has(term.source))) setSelected(new Set());
    else setSelected(new Set(terms.map((t) => t.source)));
  };

  // close export menu on outside click
  const handleExportBlur = useCallback((e: React.FocusEvent) => {
    if (
      exportRef.current &&
      !exportRef.current.contains(e.relatedTarget as Node)
    ) {
      setExportMenuOpen(false);
    }
  }, []);

  return (
    <>
      <PageHeader
        title={tr("common.glossary")}
        actions={
          <GlossaryActions
            pid={pid}
            busy={busy}
            menuOpen={exportMenuOpen}
            containerRef={exportRef}
            onToggleMenu={() => setExportMenuOpen(!exportMenuOpen)}
            onCloseMenu={() => setExportMenuOpen(false)}
            onBlur={handleExportBlur}
            onImport={() => setImportOpen(true)}
            onAdd={() => setAddOpen(true)}
          />
        }
      />
      <PageContainer className="space-y-4">
        <ErrorNotice error={termsError || conflictsError} />
        {busy && <ReadOnlyNotice />}
        <div className="flex flex-wrap items-center gap-2">
          <Input
            placeholder={tr(
              showReading
                ? "glossary.searchWithReadings"
                : "glossary.searchSourceTermsTranslationsOrAliases",
            )}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            className="min-w-40 max-w-xs flex-1"
          />
          <Select
            value={type}
            onValueChange={setType}
            aria-label={tr("common.type")}
            className="w-[140px] shrink-0"
          >
            <SelectItem value="">{tr("glossary.allTypes")}</SelectItem>
            {Object.entries(termTypes()).map(([id, label]) => (
              <SelectItem key={id} value={id}>
                {label}
              </SelectItem>
            ))}
          </Select>
          <div className="ml-auto max-w-full">
            <TermPagination
              page={page}
              total={termPage?.total || 0}
              disabled={termsLoading}
              onChange={changePage}
            />
          </div>
        </div>

        {selected.size > 0 && (
          <SelectionBar
            count={selected.size}
            busy={busy}
            onDelete={() => batchDelete.mutate()}
            onClear={() => setSelected(new Set())}
          />
        )}

        <TermTable
          terms={terms}
          selected={selected}
          loading={termsLoading}
          readOnly={busy}
          searchQuery={searchQuery}
          type={type}
          onToggleSelect={toggleSelect}
          onToggleSelectAll={toggleSelectAll}
          onEdit={setEditTerm}
          onDelete={(source) => del.mutate(source)}
        />

        {conflicts && conflicts.length > 0 && (
          <ConflictList
            pid={pid}
            conflicts={conflicts}
            busy={busy}
            onResolved={invalidate}
          />
        )}

        <SelectDismissScope dismissKey={busy ? "read-only" : "editable"}>
          <TermDetailsDialog
            pid={pid}
            term={editTerm}
            showReading={showReading}
            readOnly={busy}
            onClose={() => setEditTerm(null)}
            onSaved={() => {
              invalidate();
              setEditTerm(null);
            }}
          />
        </SelectDismissScope>
        {!busy && (
          <>
            <AddTermDialog
              pid={pid}
              open={addOpen}
              showReading={showReading}
              onClose={() => setAddOpen(false)}
              onSaved={() => {
                invalidate();
                setAddOpen(false);
              }}
            />
            <ImportDialog
              pid={pid}
              open={importOpen}
              onClose={() => setImportOpen(false)}
              onSaved={() => {
                invalidate();
                setImportOpen(false);
              }}
            />
          </>
        )}
      </PageContainer>
    </>
  );
}
