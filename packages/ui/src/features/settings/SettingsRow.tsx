import type { ReactNode } from "react";
import { Label } from "../../components/ui/form";
import { cn } from "../../lib/utils";

/** Compact control tier shared by settings pages (32px, aligned with the zcode study). */
export const SETTINGS_CONTROL_CLASSNAME = "h-8";

/** Centered reading column that carries the vertical block rhythm on settings pages. */
export const SETTINGS_CONTENT_CLASSNAME =
  "mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8";

/**
 * One settings row: label plus optional description on the left, control on the
 * right, vertically centered on a 12px rhythm with hairline dividers between
 * sibling rows. Rows carry their own horizontal padding; stack them inside a
 * bordered group card at the call site.
 */
export function SettingsRow({
  label,
  htmlFor,
  description,
  control,
  detail,
  className,
  controlClassName,
}: {
  label: ReactNode;
  htmlFor?: string;
  description?: ReactNode;
  control: ReactNode;
  detail?: ReactNode;
  className?: string;
  controlClassName?: string;
}) {
  return (
    <div className={cn("border-t border-border px-4 py-3 first:border-t-0", className)}>
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
        <div className={cn("flex w-full max-w-80 shrink-0 items-center justify-end gap-2 sm:w-80", controlClassName)}>{control}</div>
      </div>
      {detail != null && <div className="mt-3 min-w-0">{detail}</div>}
    </div>
  );
}
