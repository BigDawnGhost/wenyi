import type { ReactNode } from "react";
import { Label } from "../../components/ui/form";
import { cn } from "../../lib/utils";

/** Compact control tier shared by settings pages (32px, aligned with the zcode study). */
export const SETTINGS_CONTROL_CLASSNAME = "h-8";

/** Centered reading column that carries the vertical block rhythm on settings pages. */
export const SETTINGS_CONTENT_CLASSNAME =
  "mx-auto flex w-full max-w-4xl flex-1 flex-col gap-8";

/**
 * One settings row: label plus optional description on the left, control on the
 * right, vertically centered on a 12px rhythm with hairline dividers between
 * sibling rows. Wrap groups of rows in a bare bordered card at the call site.
 */
export function SettingsRow({
  label,
  htmlFor,
  description,
  control,
  detail,
  className,
}: {
  label: ReactNode;
  htmlFor?: string;
  description?: ReactNode;
  control: ReactNode;
  detail?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("border-t border-border py-3 first:border-t-0", className)}>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1 space-y-1">
          {htmlFor ? (
            <Label htmlFor={htmlFor}>{label}</Label>
          ) : (
            <p className="text-sm font-medium">{label}</p>
          )}
          {description != null && (
            <p className="text-sm leading-6 text-muted-foreground">{description}</p>
          )}
        </div>
        <div className="flex w-48 shrink-0 items-center justify-end gap-2">{control}</div>
      </div>
      {detail != null && <div className="mt-3 min-w-0">{detail}</div>}
    </div>
  );
}
