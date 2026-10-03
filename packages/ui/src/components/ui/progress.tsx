import * as React from "react";
import { cn } from "@/lib/utils";

export const Progress = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { value?: number; max?: number }
>(({ className, value = 0, max = 100, ...p }, ref) => {
  const pct = Math.min(100, Math.max(0, (Number(value) / Math.max(1, Number(max))) * 100));
  return (
    <div ref={ref} className={cn("relative h-2 w-full overflow-hidden rounded-full bg-secondary", className)} {...p}>
      <div className="h-full bg-primary transition-all" style={{ width: `${pct}%` }} />
    </div>
  );
});
Progress.displayName = "Progress";

export const Switch = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & { checked?: boolean }
>(({ className, checked, ...p }, ref) => (
  <button
    ref={ref}
    type="button"
    role="switch"
    aria-checked={checked}
    className={cn(
      "peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors disabled:opacity-50",
      checked ? "bg-primary" : "bg-input",
      className
    )}
    {...p}
  >
    <span className={cn("pointer-events-none block h-4 w-4 rounded-full bg-background shadow ring-0 transition-transform", checked ? "translate-x-4" : "translate-x-0")} />
  </button>
));
Switch.displayName = "Switch";
