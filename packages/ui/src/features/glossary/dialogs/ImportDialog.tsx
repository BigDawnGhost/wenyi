import { useEffect, useRef, useState } from "react";
import { Upload } from "lucide-react";
import { api } from "../../../api/client";
import {
  glossaryImportBody,
  MAX_GLOSSARY_IMPORT_BYTES,
  MAX_GLOSSARY_IMPORT_MIB,
} from "../../../api/glossary-import";
import type { Term } from "../../../api/types";
import { useI18n } from "../../../i18n";
import { errorMessage, isErrorEnvelope } from "../../../lib/error-codes";
import { toast } from "../../../lib/toast";
import { cn } from "../../../lib/utils";
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
  onChanged,
}: {
  pid: string;
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t: tr } = useI18n();
  const [step, setStep] = useState<
    "upload" | "checking" | "conflicts" | "importing"
  >("upload");
  const [parsed, setParsed] = useState<Partial<Term>[]>([]);
  const [conflicts, setConflicts] = useState<ImportConflict[]>([]);
  const [conflictPage, setConflictPage] = useState(0);
  const [importError, setImportError] = useState("");
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
    setImportError("");
  };
  useEffect(() => {
    if (!open) operation.current++;
    return () => {
      operation.current++;
    };
  }, [open]);

  const requestTooLarge = () =>
    tr("glossary.importRequestTooLarge", { limit: MAX_GLOSSARY_IMPORT_MIB });

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const attempt = ++operation.current;
    if (file.size > MAX_GLOSSARY_IMPORT_BYTES) {
      setImportError(tr("glossary.importFileTooLarge", { limit: MAX_GLOSSARY_IMPORT_MIB }));
      return;
    }
    setImportError("");
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
      const body = glossaryImportBody(items);
      if (body === null) {
        setImportError(requestTooLarge());
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
      if (found.length > 0) {
        setParsed(items);
        setConflicts(found);
        setStep("conflicts");
      } else {
        void doImport(body);
      }
    };
    reader.readAsText(file);
  };

  const finishImport = (imported: number) => {
    toast.success(tr("glossary.importedTerms", { count: imported }));
    reset();
    onClose();
  };

  const doImport = async (body: string) => {
    const attempt = ++operation.current;
    setStep("importing");
    let result: { imported: number };
    try {
      result = await api.importGlossary(pid, body);
    } catch (e) {
      if (attempt !== operation.current) return;
      const status = isErrorEnvelope(e) ? e.status : undefined;
      const error = status === 413
        ? requestTooLarge()
        : errorMessage(status === 422 ? { code: "validation_failed" } : e, tr);
      reset();
      setImportError(tr("glossary.couldNotImportTerms", { error }));
      return;
    }
    onChanged();
    if (attempt === operation.current) finishImport(result.imported);
  };

  const confirmImport = () => {
    const skipSources = new Set(
      conflicts.filter((c) => c.decision === "skip").map((c) => c.source),
    );
    const selected = parsed.filter((term) => !skipSources.has(term.source!));
    if (selected.length === 0) {
      finishImport(0);
      return;
    }
    const body = glossaryImportBody(selected);
    if (body === null) {
      reset();
      setImportError(requestTooLarge());
      return;
    }
    void doImport(body);
  };

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
      closeDisabled={step === "importing"}
      onClose={() => {
        reset();
        onClose();
      }}
    >
      {step === "upload" && (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            {tr("glossary.importTermsFromCsvOrJsonCsv", { limit: MAX_GLOSSARY_IMPORT_MIB })}
          </p>
          {importError && <p role="alert" className="text-sm text-destructive">{importError}</p>}
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
                <span className="font-medium min-w-20">{c.source}</span>
                <span className="text-muted-foreground">
                  {tr("glossary.current")}
                  {c.existingTarget}
                </span>
                <span className="text-muted-foreground">
                  {tr("glossary.imported")}
                  {c.newTarget}
                </span>
                <button
                  className={cn("ml-auto rounded-md border px-2 py-0.5 text-xs font-medium", c.decision === "overwrite" ? "border-transparent bg-primary text-primary-foreground" : "border-border bg-secondary text-secondary-foreground")}
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
            <Button onClick={confirmImport}>
              {tr("glossary.confirmImport")}
            </Button>
          </div>
        </div>
      )}

      {(step === "importing" || step === "checking") && (
        <div role="status" className="py-8 text-center text-sm text-muted-foreground">
          {tr(step === "checking" ? "glossary.checkingTerms" : "glossary.importing")}
        </div>
      )}
    </Dialog>
  );
}
