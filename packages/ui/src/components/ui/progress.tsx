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
