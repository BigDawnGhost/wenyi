import { useI18n, translate as tr } from "@/i18n";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { api, isProjectBusy, type Term } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Label } from "@/components/ui/form";
import { Select, SelectItem } from "@/components/ui/select";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ErrorNotice, ReadOnlyNotice } from "@/components/ui/data";
import { Dialog } from "@/components/ui/misc";
import {
  Plus,
  Trash2,
  Pencil,
  Download,
  Upload,
  ChevronDown,
} from "lucide-react";

const termTypes = (): Record<string, string> => ({
  person: tr("glossary.person"),
  term: tr("common.terms"),
  appellation: tr("glossary.appellation"),
  honorific: tr("glossary.honorific"),
  speech: tr("glossary.speechHabit"),
  fixed_expression: tr("glossary.fixedExpression"),
});

const PAGE_SIZE = 100;

function TermPagination({
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

// ── CSV helpers ─────────────────────────────────────────────────────────
function parseCsv(text: string): Partial<Term>[] {
  const rows: string[][] = [];
  let row: string[] = [],
    value = "",
    quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') {
        value += '"';
        i++;
      } else quoted = !quoted;
    } else if (!quoted && char === ",") {
      row.push(value);
      value = "";
    } else if (!quoted && char === "\n") {
      row.push(value.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      value = "";
    } else value += char;
  }
  if (quoted) throw new Error(tr("glossary.unclosedQuoteInCsv"));
  if (value || row.length) {
    row.push(value.replace(/\r$/, ""));
    rows.push(row);
  }
  const headers = (rows.shift() || []).map((h) =>
    h.trim().replace(/^\uFEFF/, ""),
  );
  return rows
    .filter((values) => values.some(Boolean))
    .map((vals) => {
      const row: Record<string, unknown> = {};
      headers.forEach((h, i) => {
        row[h] = vals[i] ?? "";
      });
      return {
        source: String(row.source ?? ""),
        target: String(row.target ?? ""),
        reading: String(row.reading ?? ""),
        type: String(row.type ?? "term"),
        gender: String(row.gender ?? ""),
        note: String(row.note ?? ""),
        aliases: row.aliases
          ? String(row.aliases).split("|").filter(Boolean)
          : [],
      };
    });
}

// ── Page ────────────────────────────────────────────────────────────────
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

  const { data: project } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
  });
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
    onError: (e) => toast.error(e.message),
  });
  const resolve = useMutation({
    mutationFn: ({
      cid,
      decision,
      target,
    }: {
      cid: number;
      decision: string;
      target?: string;
    }) => api.resolveConflict(pid, cid, { decision, target }),
    onSuccess: () => {
      invalidate();
      toast.success(tr("glossary.conflictResolved"));
    },
    onError: (e) => toast.error(e.message),
  });

  // batch ops
  const batchDelete = useMutation({
    mutationFn: () =>
      Promise.all([...selected].map((s) => api.deleteTerm(pid, s))),
    onSuccess: () => {
      invalidate();
      toast.success(tr("glossary.selectedTermsDeleted"));
    },
    onError: () => toast.error(tr("glossary.couldNotDeleteSelectedTerms")),
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

  // export
  const handleExport = (fmt: "json" | "csv") => {
    setExportMenuOpen(false);
    api
      .downloadGlossary(pid, fmt)
      .catch((error: Error) => toast.error(error.message));
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
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative" ref={exportRef} onBlur={handleExportBlur}>
              <Button
                variant="outline"
                onClick={() => setExportMenuOpen(!exportMenuOpen)}
              >
                <Download className="h-4 w-4" />
                {tr("common.export")} <ChevronDown className="h-3 w-3" />
              </Button>
              {exportMenuOpen && (
                <div className="absolute right-0 top-full z-10 mt-1 w-36 rounded-md border bg-popover p-1 shadow-md">
                  <button
                    className="w-full rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => handleExport("csv")}
                  >
                    {tr("glossary.csvFile")}
                  </button>
                  <button
                    className="w-full rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => handleExport("json")}
                  >
                    {tr("glossary.jsonFile")}
                  </button>
                </div>
              )}
            </div>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setImportOpen(true)}
            >
              <Upload className="h-4 w-4" />
              {tr("glossary.import")}
            </Button>
            <Button disabled={busy} onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" />
              {tr("glossary.addTerm")}
            </Button>
          </div>
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

        {/* batch action bar */}
        {selected.size > 0 && (
          <div className="flex items-center gap-3 rounded-md border bg-muted/60 px-4 py-2 text-sm">
            <span>
              {tr("glossary.selectedCount", { count: selected.size })}
            </span>
            <Button
              size="sm"
              variant="destructive"
              disabled={busy}
              onClick={() => {
                if (
                  confirm(
                    tr("glossary.deleteSelectedTerms", {
                      count: selected.size,
                    }),
                  )
                )
                  batchDelete.mutate();
              }}
            >
              <Trash2 className="h-3.5 w-3.5" />
              {tr("glossary.deleteSelected")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setSelected(new Set())}
            >
              {tr("glossary.clearSelection")}
            </Button>
          </div>
        )}

        <Card>
          <CardContent className="overflow-x-auto p-0">
            <table className="w-full min-w-[640px] table-fixed text-sm">
              <colgroup>
                <col className="w-10" />
                <col />
                <col />
                <col className="w-36" />
                <col className="w-24" />
              </colgroup>
              <thead className="whitespace-nowrap border-b text-xs text-muted-foreground">
                <tr>
                  <th className="text-left p-3 font-medium">
                    <input
                      type="checkbox"
                      aria-label={tr("glossary.selectPage")}
                      disabled={termsLoading || !terms?.length}
                      checked={
                        terms
                          ? terms.length > 0 && terms.every((term) => selected.has(term.source))
                          : false
                      }
                      onChange={toggleSelectAll}
                    />
                  </th>
                  <th className="text-left p-3 font-medium">
                    {tr("glossary.sourceTerm")}
                  </th>
                  <th className="text-left p-3 font-medium">
                    {tr("glossary.translatedTerm")}
                  </th>
                  <th className="text-left p-3 font-medium">
                    {tr("common.type")}
                  </th>
                  <th className="text-right p-3 font-medium">
                    {tr("common.actions")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {(terms || []).map((t: Term) => (
                  <tr
                    key={t.source}
                    className="border-b last:border-0 hover:bg-muted/40"
                  >
                    <td className="p-3">
                      <input
                        type="checkbox"
                        checked={selected.has(t.source)}
                        onChange={() => toggleSelect(t.source)}
                      />
                    </td>
                    <td className="p-3 font-medium">
                      <span className="block truncate" title={t.source}>
                        {t.source}
                      </span>
                    </td>
                    <td className="p-3">
                      <span className="block truncate" title={t.target}>
                        {t.target}
                      </span>
                    </td>
                    <td className="p-3">
                      <Badge variant="outline">
                        {termTypes()[t.type || ""] || t.type}
                      </Badge>
                    </td>
                    <td className="p-3 text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          disabled={busy}
                          aria-label={tr("glossary.editTerm")}
                          onClick={() => setEditTerm(t)}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          disabled={busy}
                          aria-label={tr("glossary.deleteTerm")}
                          onClick={() => del.mutate(t.source)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
                {terms && terms.length === 0 && (
                  <tr>
                    <td
                      colSpan={5}
                      className="p-8 text-center text-muted-foreground text-sm"
                    >
                      {tr(
                        searchQuery || type
                          ? "glossary.noMatchingTerms"
                          : "glossary.noTermsYetTermsAreExtractedDuring",
                      )}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </CardContent>
        </Card>

        {conflicts && conflicts.length > 0 && (
          <Card className="border-amber-300 bg-amber-50 dark:bg-amber-950/20">
            <CardContent className="p-4 space-y-2">
              <div className="font-medium text-sm">
                {tr("glossary.conflictCount", { count: conflicts.length })}
              </div>
              {conflicts.map((c) => (
                <div
                  key={c.id}
                  className="flex flex-wrap items-center gap-3 text-sm border-t pt-2"
                >
                  <span className="font-medium">{c.source}</span>
                  <span className="text-muted-foreground">
                    {tr("glossary.current")}
                    {c.existing_target}
                  </span>
                  <span className="text-muted-foreground">
                    {tr("glossary.aiSuggestion")}
                    {c.proposed_target}
                  </span>
                  <span className="flex gap-1 ml-auto">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() =>
                        resolve.mutate({ cid: c.id, decision: "current" })
                      }
                    >
                      {tr("glossary.keepCurrent")}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() =>
                        resolve.mutate({ cid: c.id, decision: "proposed" })
                      }
                    >
                      {tr("glossary.acceptSuggestion")}
                    </Button>
                  </span>
                </div>
              ))}
            </CardContent>
          </Card>
        )}

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
            <EditTermDialog
              pid={pid}
              term={editTerm}
              showReading={showReading}
              onClose={() => setEditTerm(null)}
              onSaved={() => {
                invalidate();
                setEditTerm(null);
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

// ── Add Term Dialog ─────────────────────────────────────────────────────
function AddTermDialog({
  pid,
  open,
  showReading,
  onClose,
  onSaved,
}: {
  pid: string;
  open: boolean;
  showReading: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t: tr } = useI18n();
  const [form, setForm] = useState({
    source: "",
    target: "",
    reading: "",
    type: "term",
    note: "",
  });
  const m = useMutation({
    mutationFn: () => api.addTerm(pid, form),
    onSuccess: () => {
      toast.success(tr("glossary.added"));
      setForm({ source: "", target: "", reading: "", type: "term", note: "" });
      onSaved();
    },
    onError: (e) =>
      toast.error(
        tr("glossary.couldNotAddTerm", { error: (e as Error).message }),
      ),
  });
  const resetMutation = m.reset;
  useEffect(() => {
    if (open) resetMutation();
  }, [open, resetMutation]);
  return (
    <Dialog open={open} onClose={onClose} title={tr("glossary.addTerm")}>
      {m.error && (
        <p role="alert" className="mb-3 text-sm text-destructive">
          {tr("glossary.couldNotAddTerm", { error: m.error.message })}
        </p>
      )}
      <div className="space-y-3">
        <div>
          <Label>{tr("glossary.requiredSourceTerm")}</Label>
          <Input
            value={form.source}
            onChange={(e) => setForm({ ...form, source: e.target.value })}
          />
        </div>
        <div>
          <Label>{tr("glossary.requiredTranslatedTerm")}</Label>
          <Input
            value={form.target}
            onChange={(e) => setForm({ ...form, target: e.target.value })}
          />
        </div>
        <div className={showReading ? "grid grid-cols-2 gap-3" : undefined}>
          {showReading && (
            <div>
              <Label>{tr("glossary.reading")}</Label>
              <Input
                aria-label={tr("glossary.reading")}
                value={form.reading}
                onChange={(e) => setForm({ ...form, reading: e.target.value })}
              />
            </div>
          )}
          <div>
            <Label>{tr("common.type")}</Label>
            <Select
              value={form.type}
              aria-label={tr("common.type")}
              onValueChange={(value) => setForm({ ...form, type: value })}
            >
              {Object.entries(termTypes()).map(([id, label]) => (
                <SelectItem key={id} value={id}>
                  {label}
                </SelectItem>
              ))}
            </Select>
          </div>
        </div>
        <div>
          <Label>{tr("glossary.notes")}</Label>
          <Textarea
            value={form.note}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2 mt-4">
        <Button variant="outline" onClick={onClose}>
          {tr("common.cancel")}
        </Button>
        <Button
          onClick={() => m.mutate()}
          disabled={!form.source || !form.target || m.isPending}
        >
          {tr("glossary.add")}
        </Button>
      </div>
    </Dialog>
  );
}

// ── Edit Term Dialog ────────────────────────────────────────────────────
function EditTermDialog({
  pid,
  term,
  showReading,
  onClose,
  onSaved,
}: {
  pid: string;
  term: Term | null;
  showReading: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t: tr } = useI18n();
  const [form, setForm] = useState({
    source: "",
    target: "",
    reading: "",
    type: "term",
    note: "",
    gender: "",
    aliases: "",
  });

  useEffect(() => {
    if (term) {
      setForm({
        source: term.source,
        target: term.target,
        reading: term.reading || "",
        type: term.type || "term",
        note: term.note || "",
        gender: term.gender || "",
        aliases: (term.aliases || []).join(", "),
      });
    }
  }, [term]);

  const m = useMutation({
    mutationFn: () =>
      api.updateTerm(pid, term!.source, {
        ...form,
        aliases: form.aliases
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      toast.success(tr("glossary.updated"));
      onSaved();
    },
    onError: (e) =>
      toast.error(
        tr("glossary.couldNotUpdateTerm", { error: (e as Error).message }),
      ),
  });

  const resetMutation = m.reset;
  useEffect(() => {
    resetMutation();
  }, [term, resetMutation]);

  if (!term) return null;

  return (
    <Dialog open={!!term} onClose={onClose} title={tr("glossary.editTerm")}>
      {m.error && (
        <p role="alert" className="mb-3 text-sm text-destructive">
          {tr("glossary.couldNotUpdateTerm", { error: m.error.message })}
        </p>
      )}
      <div className="space-y-3">
        <div>
          <Label>{tr("glossary.requiredSourceTerm")}</Label>
          <Input
            value={form.source}
            onChange={(e) => setForm({ ...form, source: e.target.value })}
          />
        </div>
        <div>
          <Label>{tr("glossary.requiredTranslatedTerm")}</Label>
          <Input
            value={form.target}
            onChange={(e) => setForm({ ...form, target: e.target.value })}
          />
        </div>
        <div className={showReading ? "grid grid-cols-2 gap-3" : undefined}>
          {showReading && (
            <div>
              <Label>{tr("glossary.reading")}</Label>
              <Input
                aria-label={tr("glossary.reading")}
                value={form.reading}
                onChange={(e) => setForm({ ...form, reading: e.target.value })}
              />
            </div>
          )}
          <div>
            <Label>{tr("common.type")}</Label>
            <Select
              value={form.type}
              aria-label={tr("common.type")}
              onValueChange={(value) => setForm({ ...form, type: value })}
            >
              {Object.entries(termTypes()).map(([id, label]) => (
                <SelectItem key={id} value={id}>
                  {label}
                </SelectItem>
              ))}
            </Select>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>{tr("common.gender")}</Label>
            <Select
              value={form.gender}
              aria-label={tr("common.gender")}
              onValueChange={(value) => setForm({ ...form, gender: value })}
            >
              <SelectItem value="">{tr("glossary.unspecified")}</SelectItem>
              <SelectItem value="male">{tr("glossary.male")}</SelectItem>
              <SelectItem value="female">{tr("glossary.female")}</SelectItem>
              <SelectItem value="other">{tr("glossary.other")}</SelectItem>
              <SelectItem value="unknown">{tr("glossary.unknown")}</SelectItem>
            </Select>
          </div>
        </div>
        <div>
          <Label>{tr("glossary.aliasesCommaSeparated")}</Label>
          <Input
            value={form.aliases}
            onChange={(e) => setForm({ ...form, aliases: e.target.value })}
            placeholder={tr("glossary.aliasPlaceholder")}
          />
        </div>
        <div>
          <Label>{tr("glossary.notes")}</Label>
          <Textarea
            value={form.note}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2 mt-4">
        <Button variant="outline" onClick={onClose}>
          {tr("common.cancel")}
        </Button>
        <Button
          onClick={() => m.mutate()}
          disabled={!form.source || !form.target || m.isPending}
        >
          {tr("common.save")}
        </Button>
      </div>
    </Dialog>
  );
}

// ── Import Dialog ───────────────────────────────────────────────────────
interface ImportConflict {
  source: string;
  existingTarget: string;
  newTarget: string;
  decision: "skip" | "overwrite";
}

function ImportDialog({
  pid,
  open,
  onClose,
  onSaved,
}: {
  pid: string;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t: tr } = useI18n();
  const [step, setStep] = useState<
    "upload" | "checking" | "conflicts" | "importing"
  >("upload");
  const [parsed, setParsed] = useState<Partial<Term>[]>([]);
  const [conflicts, setConflicts] = useState<ImportConflict[]>([]);
  const [conflictPage, setConflictPage] = useState(0);
  const pageConflicts = conflicts.slice(
    conflictPage * PAGE_SIZE,
    (conflictPage + 1) * PAGE_SIZE,
  );
  const fileRef = useRef<HTMLInputElement>(null);
  const operation = useRef(0);

  const reset = () => {
    operation.current++;
    setStep("upload");
    setParsed([]);
    setConflicts([]);
    setConflictPage(0);
  };
  useEffect(() => {
    if (!open) operation.current++;
    return () => {
      operation.current++;
    };
  }, [open]);

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const attempt = ++operation.current;
    const reader = new FileReader();
    reader.onload = async () => {
      if (attempt !== operation.current) return;
      const text = String(reader.result);
      let items: Partial<Term>[] = [];
      if (file.name.endsWith(".csv")) {
        try {
          items = parseCsv(text);
        } catch (error) {
          toast.error((error as Error).message);
          return;
        }
      } else {
        try {
          items = JSON.parse(text);
        } catch {
          toast.error(tr("glossary.couldNotParseJson"));
          return;
        }
      }
      if (
        !Array.isArray(items) ||
        items.some(
          (item) =>
            !item ||
            typeof item.source !== "string" ||
            typeof item.target !== "string",
        )
      ) {
        toast.error(tr("glossary.theTermFileMustBeAList"));
        return;
      }
      if (items.length === 0) {
        toast.error(tr("glossary.theFileIsEmptyOrHasAn"));
        return;
      }
      // Look up only imported sources, across every page and regardless of active filters.
      setStep("checking");
      const sources = [...new Set(items.map((item) => item.source!))];
      const existingMap = new Map<string, string>();
      try {
        for (let offset = 0; offset < sources.length; offset += 500) {
          const matches = await api.lookupTerms(pid, sources.slice(offset, offset + 500));
          if (attempt !== operation.current) return;
          for (const term of matches) existingMap.set(term.source, term.target);
        }
      } catch (error) {
        if (attempt !== operation.current) return;
        toast.error(
          tr("glossary.couldNotCheckTerms", { error: (error as Error).message }),
        );
        setStep("upload");
        return;
      }
      const found: ImportConflict[] = [];
      for (const it of items) {
        if (it.source && existingMap.has(it.source)) {
          found.push({
            source: it.source,
            existingTarget: existingMap.get(it.source)!,
            newTarget: it.target || "",
            decision: "overwrite",
          });
        }
      }
      setParsed(items);
      if (found.length > 0) {
        setConflicts(found);
        setStep("conflicts");
      } else {
        doImport(items);
      }
    };
    reader.readAsText(file);
  };

  const doImport = (items?: Partial<Term>[]) => {
    setStep("importing");
    const toImport = items || parsed;
    // apply conflict decisions: filter out skipped
    const skipSources = new Set(
      conflicts.filter((c) => c.decision === "skip").map((c) => c.source),
    );
    const filtered = toImport.filter((t) => !skipSources.has(t.source!));
    m.mutate(filtered);
  };

  const m = useMutation({
    mutationFn: (items: Partial<Term>[]) => api.importGlossary(pid, items),
    onSuccess: (res) => {
      toast.success(tr("glossary.importedTerms", { count: res.imported }));
      reset();
      onSaved();
    },
    onError: (e) => {
      toast.error(
        tr("glossary.couldNotImportTerms", { error: (e as Error).message }),
      );
      setStep("upload");
    },
  });

  const toggleConflict = (idx: number) => {
    setConflicts((prev) =>
      prev.map((c, i) =>
        i === idx
          ? { ...c, decision: c.decision === "skip" ? "overwrite" : "skip" }
          : c,
      ),
    );
  };

  return (
    <Dialog
      open={open}
      title={tr("glossary.importTerms")}
      onClose={() => {
        reset();
        onClose();
      }}
    >
      {step === "upload" && (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            {tr("glossary.importTermsFromCsvOrJsonCsv")}
          </p>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,.json"
            className="hidden"
            onChange={handleFile}
          />
          <Button onClick={() => fileRef.current?.click()}>
            <Upload className="h-4 w-4" />
            {tr("glossary.chooseFile")}
          </Button>
        </div>
      )}

      {step === "conflicts" && (
        <div className="space-y-4">
          <p className="text-sm">
            {tr("glossary.importConflictCount", { count: conflicts.length })}
          </p>
          <div className="max-h-60 overflow-auto space-y-2">
            {pageConflicts.map((c, i) => (
              <div
                key={c.source}
                className="flex items-center gap-3 rounded border p-2 text-sm"
              >
                <span className="font-medium min-w-[80px]">{c.source}</span>
                <span className="text-muted-foreground">
                  {tr("glossary.current")}
                  {c.existingTarget}
                </span>
                <span className="text-muted-foreground">
                  {tr("glossary.imported")}
                  {c.newTarget}
                </span>
                <button
                  className={`ml-auto rounded px-2 py-0.5 text-xs ${c.decision === "overwrite" ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"}`}
                  onClick={() => toggleConflict(conflictPage * PAGE_SIZE + i)}
                >
                  {c.decision === "overwrite"
                    ? tr("glossary.overwrite")
                    : tr("glossary.skip")}
                </button>
              </div>
            ))}
          </div>
          <TermPagination
            page={conflictPage}
            total={conflicts.length}
            onChange={setConflictPage}
          />
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              onClick={() => {
                reset();
                onClose();
              }}
            >
              {tr("common.cancel")}
            </Button>
            <Button onClick={() => doImport()}>
              {tr("glossary.confirmImport")}
            </Button>
          </div>
        </div>
      )}

      {(step === "importing" || step === "checking") && (
        <div className="py-8 text-center text-sm text-muted-foreground">
          {tr(step === "checking" ? "glossary.checkingTerms" : "glossary.importing")}
        </div>
      )}
    </Dialog>
  );
}
