import { Progress } from "@wenyi/ui/primitives";
import { formatBytes } from "@wenyi/ui/format";
import { useDesktopI18n } from "./i18n";

/** One transfer presentation for app packages, plugin runtimes, and model assets. */
export function TransferProgress({ label, downloaded, total }: {
  label: string; downloaded?: number; total?: number | null;
}) {
  const { locale, t } = useDesktopI18n();
  const count = Math.max(0, downloaded ?? 0);
  const known = total !== undefined && total !== null && total > 0;
  const percent = known ? Math.min(100, Math.floor(count / total * 100)) : undefined;
  return (
    <div className="min-w-0 space-y-3 rounded-lg border bg-muted/30 p-4" data-slot="transfer-progress">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span role="status" className="font-medium">{label}</span>
        {percent !== undefined && (
          <span className="tabular-nums text-muted-foreground">
            {new Intl.NumberFormat(locale, { style: "percent" }).format(percent / 100)}
          </span>
        )}
      </div>
      <Progress aria-label={label} value={percent} indeterminate={!known} />
      {downloaded !== undefined && (
        <p className="text-xs tabular-nums text-muted-foreground">
          {known
            ? `${count === 0 ? "0 B" : formatBytes(count)} / ${formatBytes(total)}`
            : t("transfer.downloaded", { size: count === 0 ? "0 B" : formatBytes(count) })}
        </p>
      )}
    </div>
  );
}
