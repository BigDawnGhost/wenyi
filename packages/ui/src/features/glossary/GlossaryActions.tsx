import { useMutation } from "@tanstack/react-query";
import { ChevronDown, Download, Plus, Upload } from "lucide-react";
import { api } from "../../api/client";
import type { GlossaryExportFormat } from "../../api/types";
import { useI18n } from "../../i18n";
import { errorMessage } from "../../lib/error-codes";
import { toast } from "../../lib/toast";
import { Button } from "../../components/ui/button";

/** Header actions: export menu, import and add. */
export function GlossaryActions({
  pid,
  busy,
  menuOpen,
  containerRef,
  onToggleMenu,
  onCloseMenu,
  onBlur,
  onImport,
  onAdd,
}: {
  pid: string;
  busy: boolean;
  menuOpen: boolean;
  containerRef: React.RefObject<HTMLDivElement | null>;
  onToggleMenu: () => void;
  onCloseMenu: () => void;
  onBlur: (e: React.FocusEvent) => void;
  onImport: () => void;
  onAdd: () => void;
}) {
  const { t: tr } = useI18n();
  const exportGlossary = useMutation({
    mutationFn: async (fmt: GlossaryExportFormat) => {
      const saved = await api.downloadGlossary(pid, fmt);
      if (saved) toast.success(tr("export.savedTo", { path: saved.path }));
    },
    onError: (error) => toast.error(errorMessage(error, tr)),
  });
  const handleExport = (fmt: GlossaryExportFormat) => {
    onCloseMenu();
    exportGlossary.mutate(fmt);
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative" ref={containerRef} onBlur={onBlur}>
        <Button
          variant="outline"
          disabled={exportGlossary.isPending}
          onClick={onToggleMenu}
        >
          <Download className="h-4 w-4" />
          {tr("common.export")} <ChevronDown className="h-3 w-3" />
        </Button>
        {menuOpen && (
          <div className="absolute right-0 top-full z-10 mt-1 w-56 rounded-md border bg-popover p-1 shadow-md">
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
            <button
              className="w-full rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
              onClick={() => handleExport("apkg")}
            >
              {tr("glossary.ankiDeck")}
            </button>
          </div>
        )}
      </div>
      <Button variant="outline" disabled={busy} onClick={onImport}>
        <Upload className="h-4 w-4" />
        {tr("glossary.import")}
      </Button>
      <Button disabled={busy} onClick={onAdd}>
        <Plus className="h-4 w-4" />
        {tr("glossary.addTerm")}
      </Button>
    </div>
  );
}
