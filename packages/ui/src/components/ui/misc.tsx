import { useI18n } from "@/i18n";
import * as React from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";
import { X } from "lucide-react";

// Lightweight dialog component. Portaled to the body and rendered as a native
// modal dialog so page-container spacing (space-y margins) never offsets it.
export function Dialog({
  open,
  onClose,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  className?: string;
}) {
  const { t } = useI18n();
  const dialogRef = React.useRef<HTMLDialogElement>(null);
  React.useEffect(() => {
    if (!open) return;
    const element = dialogRef.current;
    element?.showModal();
    return () => element?.close();
  }, [open]);
  if (!open) return null;
  return createPortal(
    <dialog
      ref={dialogRef}
      className={cn(
        "fixed inset-0 m-auto w-[calc(100%-32px)] max-w-lg max-h-[85vh] overflow-auto rounded-lg border bg-background p-6 text-foreground shadow-lg backdrop:bg-black/50 backdrop:backdrop-blur-[2px]",
        className,
      )}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (
          event.clientX < rect.left ||
          event.clientX > rect.right ||
          event.clientY < rect.top ||
          event.clientY > rect.bottom
        ) {
          onClose();
        }
      }}
    >
      <button
        aria-label={t("common.close")}
        className="absolute right-4 top-4 flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        onClick={onClose}
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
      {children}
    </dialog>,
    document.body,
  );
}

// Lightweight tab components.
const TabsCtx = React.createContext<{
  value: string;
  setValue: (v: string) => void;
}>({ value: "", setValue: () => {} });
export function Tabs({
  value,
  onValueChange,
  children,
  className,
}: {
  value: string;
  onValueChange: (v: string) => void;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <TabsCtx.Provider value={{ value, setValue: onValueChange }}>
      <div className={className}>{children}</div>
    </TabsCtx.Provider>
  );
}
export function TabsList({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "inline-flex items-center gap-1 rounded-md bg-muted p-1",
        className,
      )}
    >
      {children}
    </div>
  );
}
export function TabsTrigger({
  value,
  children,
  className,
}: {
  value: string;
  children: React.ReactNode;
  className?: string;
}) {
  const ctx = React.useContext(TabsCtx);
  const active = ctx.value === value;
  return (
    <button
      className={cn(
        "rounded px-3 py-1 text-sm transition-colors",
        active
          ? "bg-background shadow text-foreground"
          : "text-muted-foreground hover:text-foreground",
        className,
      )}
      onClick={() => ctx.setValue(value)}
    >
      {children}
    </button>
  );
}
export function TabsContent({
  value,
  children,
  className,
}: {
  value: string;
  children: React.ReactNode;
  className?: string;
}) {
  const ctx = React.useContext(TabsCtx);
  if (ctx.value !== value) return null;
  return <div className={className}>{children}</div>;
}
