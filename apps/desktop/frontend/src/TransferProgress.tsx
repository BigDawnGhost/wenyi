import type { ReactNode } from "react";
import { Progress } from "@wenyi/ui/primitives";
import { formatBytes } from "@wenyi/ui/format";
import { useDesktopI18n } from "./i18n";

/** One transfer presentation for app packages, plugin runtimes, and model assets. */
export function TransferProgress({ label, downloaded, total, actions }: {
  label: string; downloaded?: number; total?: number | null;
  /** Inline controls (for example a cancel button) kept on the label row. */
  actions?: ReactNode;
}) {
  const { locale, t } = useDesktopI18n();
  const count = Math.max(0, downloaded ?? 0);
  const known = total !== undefined && total !== null && total > 0;
  const percent = known ? Math.min(100, Math.floor(count / total * 100)) : undefined;
  return (
    // A bare progress line: every call site already sits inside a card, so a
    // nested bordered box read as a card within the card on every page.
    <div className="min-w-0 space-y-3" data-slot="transfer-progress">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span role="status" className="font-medium">{label}</span>
        <div className="flex items-center gap-2">
          {percent !== undefined && (
            <span className="tabular-nums text-muted-foreground">
              {new Intl.NumberFormat(locale, { style: "percent" }).format(percent / 100)}
            </span>
          )}
          {actions}
        </div>
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
