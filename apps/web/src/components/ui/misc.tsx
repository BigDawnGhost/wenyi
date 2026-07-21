import * as React from "react";
import { cn } from "@/lib/utils";
import { X } from "lucide-react";

// 简易 Dialog（非 Radix，足够 MVP）
export function Dialog({ open, onClose, children, className }: {
  open: boolean; onClose: () => void; children: React.ReactNode; className?: string;
}) {
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className={cn("relative w-full max-w-lg rounded-lg border bg-background p-6 shadow-lg max-h-[85vh] overflow-auto", className)}
        onClick={(e) => e.stopPropagation()}
      >
        <button className="absolute right-4 top-4 text-muted-foreground hover:text-foreground" onClick={onClose}>
          <X className="h-4 w-4" />
        </button>
        {children}
      </div>
    </div>
  );
}

// 简易 Tabs
const TabsCtx = React.createContext<{ value: string; setValue: (v: string) => void }>({ value: "", setValue: () => {} });
export function Tabs({ value, onValueChange, children, className }: {
  value: string; onValueChange: (v: string) => void; children: React.ReactNode; className?: string;
}) {
  return (
    <TabsCtx.Provider value={{ value, setValue: onValueChange }}>
      <div className={className}>{children}</div>
    </TabsCtx.Provider>
  );
}
export function TabsList({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("inline-flex items-center gap-1 rounded-md bg-muted p-1", className)}>{children}</div>;
}
export function TabsTrigger({ value, children, className }: { value: string; children: React.ReactNode; className?: string }) {
  const ctx = React.useContext(TabsCtx);
  const active = ctx.value === value;
  return (
    <button
      className={cn("rounded px-3 py-1 text-sm transition-colors", active ? "bg-background shadow text-foreground" : "text-muted-foreground hover:text-foreground", className)}
      onClick={() => ctx.setValue(value)}
    >
      {children}
    </button>
  );
}
export function TabsContent({ value, children, className }: { value: string; children: React.ReactNode; className?: string }) {
  const ctx = React.useContext(TabsCtx);
  if (ctx.value !== value) return null;
  return <div className={className}>{children}</div>;
}

// Table 基础件
export const Table = React.forwardRef<HTMLTableElement, React.HTMLAttributes<HTMLTableElement>>(
  ({ className, ...p }, ref) => (
    <div className="w-full overflow-auto">
      <table ref={ref} className={cn("w-full caption-bottom text-sm", className)} {...p} />
    </div>
  )
);
Table.displayName = "Table";
export const THead = (p: React.HTMLAttributes<HTMLTableSectionElement>) => <thead className="[&_tr]:border-b" {...p} />;
export const TBody = (p: React.HTMLAttributes<HTMLTableSectionElement>) => <tbody className="[&_tr:last-child]:border-0" {...p} />;
export const TR = (p: React.HTMLAttributes<HTMLTableRowElement>) => <tr className="border-b transition-colors hover:bg-muted/50" {...p} />;
export const TH = (p: React.ThHTMLAttributes<HTMLTableCellElement>) => <th className="h-9 px-3 text-left align-middle font-medium text-muted-foreground" {...p} />;
export const TD = (p: React.TdHTMLAttributes<HTMLTableCellElement>) => <td className="px-3 py-2 align-middle" {...p} />;
