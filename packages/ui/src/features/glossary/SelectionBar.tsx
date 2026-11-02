import { Trash2 } from "lucide-react";
import { useI18n } from "../../i18n";
import { Button } from "../../components/ui/button";

/** Batch bar shown only while at least one term is selected. */
export function SelectionBar({
  count,
  busy,
  onDelete,
  onClear,
}: {
  count: number;
  busy: boolean;
  onDelete: () => void;
  onClear: () => void;
}) {
  const { t: tr } = useI18n();
  return (
    <div className="flex items-center gap-3 rounded-md border bg-muted/60 px-4 py-2 text-sm">
      <span>{tr("glossary.selectedCount", { count })}</span>
      <Button
        size="sm"
        variant="destructive"
        disabled={busy}
        onClick={() => {
          if (confirm(tr("glossary.deleteSelectedTerms", { count }))) onDelete();
        }}
      >
        <Trash2 className="h-3.5 w-3.5" />
        {tr("glossary.deleteSelected")}
      </Button>
      <Button size="sm" variant="ghost" onClick={onClear}>
        {tr("glossary.clearSelection")}
      </Button>
    </div>
  );
}
