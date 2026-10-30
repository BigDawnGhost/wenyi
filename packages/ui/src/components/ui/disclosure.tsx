import { useEffect, useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

/** Keep controls mounted so folding a section never discards its draft values. */
export function Disclosure({
  title,
  summary,
  error,
  children,
  className,
  lazy = false,
}: {
  title: string;
  summary?: ReactNode;
  error?: unknown;
  children: ReactNode;
  className?: string;
  /** Defer read-only details until first expansion, then retain their state. */
  lazy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [visited, setVisited] = useState(false);
  useEffect(() => {
    if (error) setOpen(true);
  }, [error]);
  return (
    <details
      open={open}
      onToggle={(event) => {
        setOpen(event.currentTarget.open);
        if (event.currentTarget.open) setVisited(true);
      }}
      onInvalidCapture={() => setOpen(true)}
      className={cn("rounded-lg border p-4", className)}
    >
      <summary className="disclosure-summary cursor-pointer text-sm font-medium">
        <ChevronRight className="disclosure-chevron" aria-hidden="true" />
        <span className="min-w-0">
          {title}
          {summary != null && (
            <span className="ml-2 font-normal text-muted-foreground">
              {summary}
            </span>
          )}
        </span>
      </summary>
      {(!lazy || open || visited) && <div className="mt-4 space-y-4">{children}</div>}
    </details>
  );
}
