import { useI18n } from "@/i18n";
import * as React from "react";
import * as Primitive from "@radix-ui/react-dialog";
import { cn } from "@/lib/utils";
import { X } from "lucide-react";

export function Dialog({
  open,
  onClose,
  title,
  description,
  closeDisabled = false,
  initialFocus,
  returnFocus,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  closeDisabled?: boolean;
  initialFocus?: React.RefObject<HTMLElement | null>;
  returnFocus?: () => HTMLElement | null;
  children: React.ReactNode;
  className?: string;
}) {
  const { t } = useI18n();
  const opener = React.useRef<HTMLElement | null>(null);
  return (
    <Primitive.Root
      open={open}
      onOpenChange={(next) => {
        if (!next && !closeDisabled) onClose();
      }}
    >
      <Primitive.Portal>
        <Primitive.Overlay className="fixed inset-0 z-50 bg-black/50" />
        <Primitive.Content
          {...(!description ? { "aria-describedby": undefined } : {})}
          className={cn(
            "fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh_-_2rem)] w-[calc(100%_-_2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-lg border bg-background shadow-lg",
            className,
          )}
          onOpenAutoFocus={(event) => {
            opener.current = document.activeElement instanceof HTMLElement
              ? document.activeElement
              : null;
            if (initialFocus?.current && !initialFocus.current.matches(":disabled")) {
              event.preventDefault();
              initialFocus.current.focus();
            }
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const target = returnFocus?.() ?? opener.current;
            if (target?.isConnected) target.focus();
          }}
          onEscapeKeyDown={(event) => {
            if (closeDisabled) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (closeDisabled) event.preventDefault();
          }}
        >
          <div className="shrink-0 border-b px-4 py-4 pr-14 sm:px-6 sm:pr-14">
            <Primitive.Title className="text-lg font-semibold">{title}</Primitive.Title>
            {description && (
              <Primitive.Description className="mt-0.5 text-sm text-muted-foreground">
                {description}
              </Primitive.Description>
            )}
            <Primitive.Close
              type="button"
              disabled={closeDisabled}
              aria-label={t("common.close")}
              className="absolute right-4 top-4 flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </Primitive.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">{children}</div>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
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
