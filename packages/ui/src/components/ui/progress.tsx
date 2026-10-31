import * as React from "react";
import { cn } from "@/lib/utils";

export const Progress = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { value?: number; max?: number; indeterminate?: boolean }
>(({ className, value = 0, max = 100, indeterminate = false, ...p }, ref) => {
  const pct = Math.min(100, Math.max(0, (Number(value) / Math.max(1, Number(max))) * 100));
  return (
    <div
      ref={ref}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : pct}
      className={cn("relative h-2 w-full overflow-hidden rounded-full bg-secondary", className)}
      {...p}
    >
      <div
        className={cn(
          "h-full rounded-full bg-primary",
          indeterminate ? "w-1/3 motion-safe:animate-pulse" : "transition-[width] duration-300 motion-reduce:transition-none",
        )}
        style={indeterminate ? undefined : { width: `${pct}%` }}
      />
    </div>
  );
});
Progress.displayName = "Progress";
