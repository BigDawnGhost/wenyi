import { useI18n, translate as tr } from "@/i18n";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, isProjectBusy, type Term } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Input, Select, Textarea, Label } from "@/components/ui/form";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ErrorNotice } from "@/components/ui/data";
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
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
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
  const { data: terms, error: termsError } = useQuery({
    queryKey: ["terms", pid, q, type],
    queryFn: () =>
      api.listTerms(pid, { q: q || undefined, type: type || undefined }),
    enabled: !!pid,
  });
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
    if (selected.size === terms.length) setSelected(new Set());
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
        subtitle={tr("glossary.manageNamesAppellationsAndFixedExpressionsAnd")}
        actions={
          <div className="flex items-center gap-2">
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
        {busy && (
          <p className="text-sm text-muted-foreground">
            {tr("glossary.theGlossaryIsReadOnlyWhileA")}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Input
            placeholder={tr("glossary.searchSourceTermsTranslationsOrAliases")}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            className="max-w-xs"
          />
          <Select
            value={type}
            onChange={(e) => setType(e.target.value)}
            className="max-w-[140px]"
          >
            <option value="">{tr("glossary.allTypes")}</option>
            {Object.entries(termTypes()).map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </Select>
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
            <table className="w-full min-w-[48rem] table-fixed text-sm">
              <colgroup>
                <col className="w-10" />
                <col />
                <col />
                <col className="w-32" />
                <col className="w-36" />
                <col className="w-24" />
              </colgroup>
              <thead className="whitespace-nowrap border-b text-xs text-muted-foreground">
                <tr>
                  <th className="text-left p-3 font-medium">
                    <input
                      type="checkbox"
                      checked={
                        terms
                          ? selected.size === terms.length && terms.length > 0
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
                    {tr("glossary.reading")}
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
                    <td className="p-3 text-muted-foreground">
                      <span
                        className="block truncate"
                        title={t.reading || undefined}
                      >
                        {t.reading || "—"}
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
                      colSpan={6}
                      className="p-8 text-center text-muted-foreground text-sm"
                    >
                      {tr("glossary.noTermsYetTermsAreExtractedDuring")}
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
              onClose={() => setAddOpen(false)}
              onSaved={() => {
                invalidate();
                setAddOpen(false);
              }}
            />
            <EditTermDialog
              pid={pid}
              term={editTerm}
              onClose={() => setEditTerm(null)}
              onSaved={() => {
                invalidate();
                setEditTerm(null);
              }}
            />
            <ImportDialog
              pid={pid}
              existingTerms={terms || []}
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
  onClose,
  onSaved,
}: {
  pid: string;
  open: boolean;
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
  return (
    <Dialog open={open} onClose={onClose}>
      <div className="text-lg font-semibold mb-4">{tr("glossary.addTerm")}</div>
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
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>{tr("glossary.reading")}</Label>
            <Input
              value={form.reading}
              onChange={(e) => setForm({ ...form, reading: e.target.value })}
            />
          </div>
          <div>
            <Label>{tr("common.type")}</Label>
            <Select
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
            >
              {Object.entries(termTypes()).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
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
  onClose,
  onSaved,
}: {
  pid: string;
  term: Term | null;
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

  if (!term) return null;

  return (
    <Dialog open={!!term} onClose={onClose}>
      <div className="text-lg font-semibold mb-4">
        {tr("glossary.editTerm")}
      </div>
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
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>{tr("glossary.reading")}</Label>
            <Input
              value={form.reading}
              onChange={(e) => setForm({ ...form, reading: e.target.value })}
            />
          </div>
          <div>
            <Label>{tr("common.type")}</Label>
            <Select
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
            >
              {Object.entries(termTypes()).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </Select>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>{tr("common.gender")}</Label>
            <Select
              value={form.gender}
              onChange={(e) => setForm({ ...form, gender: e.target.value })}
            >
              <option value="">{tr("glossary.unspecified")}</option>
              <option value="male">{tr("glossary.male")}</option>
              <option value="female">{tr("glossary.female")}</option>
              <option value="other">{tr("glossary.other")}</option>
              <option value="unknown">{tr("glossary.unknown")}</option>
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
  existingTerms,
  open,
  onClose,
  onSaved,
}: {
  pid: string;
  existingTerms: Term[];
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t: tr } = useI18n();
  const [step, setStep] = useState<"upload" | "conflicts" | "importing">(
    "upload",
  );
  const [parsed, setParsed] = useState<Partial<Term>[]>([]);
  const [conflicts, setConflicts] = useState<ImportConflict[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setStep("upload");
    setParsed([]);
    setConflicts([]);
  };

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
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
      // detect conflicts
      const existingMap = new Map(
        existingTerms.map((t) => [t.source, t.target]),
      );
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
      onClose={() => {
        reset();
        onClose();
      }}
    >
      <div className="text-lg font-semibold mb-4">
        {tr("glossary.importTerms")}
      </div>

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
            {conflicts.map((c, i) => (
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
                  onClick={() => toggleConflict(i)}
                >
                  {c.decision === "overwrite"
                    ? tr("glossary.overwrite")
                    : tr("glossary.skip")}
                </button>
              </div>
            ))}
          </div>
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

      {step === "importing" && (
        <div className="py-8 text-center text-sm text-muted-foreground">
          {tr("glossary.importing")}
        </div>
      )}
    </Dialog>
  );
}
