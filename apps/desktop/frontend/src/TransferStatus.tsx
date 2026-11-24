import type { ReactNode } from "react";
import { Progress } from "@wenyi/ui/primitives";
import { formatBytes } from "@wenyi/ui/format";
import { useDesktopI18n } from "./i18n";

/** Keep status and actions in one row whether or not a transfer is active. */
export function TransferStatus({ label, statusDetail, transfer, actions }: {
  label: string | undefined;
  statusDetail?: string;
  transfer?: { downloaded?: number; total?: number | null };
  actions?: ReactNode;
}) {
  const { locale, t } = useDesktopI18n();
  const { downloaded, total } = transfer ?? {};
  const count = Math.max(0, downloaded ?? 0);
  const known = total !== undefined && total !== null && total > 0;
  const percent = known ? Math.min(100, Math.floor(count / total * 100)) : undefined;
  return (
    <div className="min-w-0 space-y-3" data-slot={transfer ? "transfer-progress" : undefined}>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-sm">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          {statusDetail && <span>{statusDetail}</span>}
          {label && (
            <span role="status" className={transfer ? "font-medium" : "text-muted-foreground"}>{label}</span>
          )}
        </div>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          {percent !== undefined && (
            <span className="tabular-nums text-muted-foreground">
              {new Intl.NumberFormat(locale, { style: "percent" }).format(percent / 100)}
            </span>
          )}
          {actions}
        </div>
      </div>
      {transfer && <Progress aria-label={label} value={percent} indeterminate={!known} />}
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
