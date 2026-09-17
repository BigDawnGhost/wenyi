import { useEffect, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Keep controls mounted so folding a section never discards its draft values. */
export function Disclosure({
  title,
  summary,
  error,
  children,
  className,
}: {
  title: string;
  summary?: ReactNode;
  error?: unknown;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (error) setOpen(true);
  }, [error]);
  return (
    <details
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      onInvalidCapture={() => setOpen(true)}
      className={cn("rounded-lg border p-4", className)}
    >
      <summary className="cursor-pointer text-sm font-medium">
        {title}
        {summary != null && (
          <span className="ml-2 font-normal text-muted-foreground">
            {summary}
          </span>
        )}
      </summary>
      <div className="mt-4 space-y-4">{children}</div>
    </details>
  );
}
