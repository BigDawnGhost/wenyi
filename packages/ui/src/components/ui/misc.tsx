import { useI18n } from "../../i18n";
import * as React from "react";
import * as Primitive from "@radix-ui/react-dialog";
import { cn } from "../../lib/utils";
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
  footer,
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
  /** Actions pinned below the scrolling body, so they stay reachable in short windows. */
  footer?: React.ReactNode;
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
        <Primitive.Overlay className="dialog-overlay fixed inset-0 z-50 bg-black/50 backdrop-blur-xs" />
        <Primitive.Content
          {...(!description ? { "aria-describedby": undefined } : {})}
          className={cn(
            "dialog-content fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh_-_theme(spacing.8))] w-[calc(100%_-_theme(spacing.8))] max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl bg-card shadow-e4",
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
            // Radix observes Escape before the input does. An open editable
            // combobox gets the first Escape to dismiss its own suggestions.
            if (closeDisabled || (event.target instanceof HTMLInputElement &&
              event.target.matches('[role="combobox"][aria-expanded="true"]'))) {
              event.preventDefault();
            }
          }}
          onPointerDownOutside={(event) => {
            // Dismissing a notification must not also dismiss the active form.
            const target = event.target;
            if (closeDisabled || (
              target instanceof Element && target.closest("[data-sonner-toaster]")
            )) event.preventDefault();
          }}
        >
          <div className="shrink-0 border-b border-subtle px-7 py-4 pr-14">
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
              className="absolute right-4 top-4 flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-hover hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </Primitive.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-6">{children}</div>
          {footer && (
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-3 border-t border-subtle px-7 py-3">
              {footer}
            </div>
          )}
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}

// Lightweight tab components.
const TabsCtx = React.createContext<{
  value: string;
  setValue: (v: string) => void;
  variant: "default" | "segmented";
}>({ value: "", setValue: () => {}, variant: "default" });
export function Tabs({
  value,
  onValueChange,
  children,
  className,
  variant = "default",
}: {
  value: string;
  onValueChange: (v: string) => void;
  children: React.ReactNode;
  className?: string;
  /** "segmented" renders the settings-style pill track (zcode adoption). */
  variant?: "default" | "segmented";
}) {
  return (
    <TabsCtx.Provider value={{ value, setValue: onValueChange, variant }}>
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
  const { variant } = React.useContext(TabsCtx);
  return (
    <div
      className={cn(
        variant === "segmented"
          ? "inline-flex h-8 items-center gap-0.5 rounded-full bg-surface p-0.5"
          : "inline-flex items-center gap-1 rounded-md bg-muted p-1",
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
  const segmented = ctx.variant === "segmented";
  return (
    <button
      className={cn(
        segmented
          ? "h-7 rounded-full px-2.5 text-sm font-medium transition-colors"
          : "rounded px-3 py-1 text-sm transition-colors",
        active
          ? segmented
            ? "bg-background text-foreground"
            : "bg-card shadow-e1 text-foreground"
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

/**
 * A list or panel with nothing to show. Centred, quiet, and the same size
 * everywhere: callers were each inventing their own padding (p-8, py-8, py-24)
 * and losing the padding entirely when the empty case lived inside a table cell.
 */
export function EmptyState({
  icon: Icon,
  children,
  className,
}: {
  /** Optional mark above the message; sized here so it stays in the icon scale. */
  icon?: React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" | "false" }>;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex flex-col items-center justify-center gap-2 py-8 text-center",
        className,
      )}
    >
      {Icon ? <Icon className="h-8 w-8 text-muted-foreground" aria-hidden="true" /> : null}
      <p className="text-sm text-muted-foreground">{children}</p>
    </div>
  );
}

/**
 * A placeholder for content that is still loading. Pulse is motion, so it is
 * gated: the block still marks the space when animation is off.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      data-slot="skeleton"
      aria-hidden="true"
      className={cn("rounded-md bg-muted motion-safe:animate-pulse", className)}
    />
  );
}
