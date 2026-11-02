import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { api } from "../../../api/client";
import type { Term } from "../../../api/types";
import { useI18n } from "../../../i18n";
import { errorMessage } from "../../../lib/error-codes";
import { toast } from "../../../lib/toast";
import { Button } from "../../../components/ui/button";
import { Dialog } from "../../../components/ui/misc";
import { parseCsv } from "../import";
import { PAGE_SIZE } from "../termTypes";
import { TermPagination } from "../TermTable";

interface ImportConflict {
  source: string;
  existingTarget: string;
  newTarget: string;
  decision: "skip" | "overwrite";
}

export function ImportDialog({
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
    // A refused or interrupted read fires neither load nor the parse guards
    // below, so without these the dialog stayed on "upload" and the user saw
    // nothing. Both paths surface the same actionable failure as a bad file.
    const failed = () => {
      if (attempt !== operation.current) return;
      setStep("upload");
      toast.error(tr("glossary.couldNotReadFile"));
    };
    reader.onerror = failed;
    reader.onabort = failed;
    reader.onload = async () => {
      if (attempt !== operation.current) return;
      const text = String(reader.result);
      let items: Partial<Term>[] = [];
      if (file.name.endsWith(".csv")) {
        try {
          items = parseCsv(text, tr);
        } catch (error) {
          toast.error(errorMessage(error, tr));
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
