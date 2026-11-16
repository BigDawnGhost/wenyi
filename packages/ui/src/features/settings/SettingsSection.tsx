import { useId, type ReactNode } from "react";
import { cn } from "../../lib/utils";

/** Full-width settings sections keep their headings above the controls. */
export function SettingsSection({
  title,
  id,
  children,
  className,
  ...rest
}: {
  title: string;
  id?: string;
  children: ReactNode;
  className?: string;
  "data-testid"?: string;
}) {
  const headingId = useId();
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      data-slot="settings.section"
      className={cn(
        "min-w-0 scroll-mt-4 py-8 first-of-type:pt-0",
        className,
      )}
      {...rest}
    >
      <h2 id={headingId} className="mb-4 text-sm font-semibold">
        {title}
      </h2>
      <div className="min-w-0 space-y-4">{children}</div>
    </section>
  );
}

/** Global and project settings show groups inline without nested cards or folding. */
export function SettingsGroup({
  title,
  summary,
  children,
}: {
  title: string;
  summary?: ReactNode;
  children: ReactNode;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="min-w-0 space-y-4">
      <h3 id={headingId} className="text-sm font-medium">
        {title}
        {summary != null && (
          <span className="ml-2 font-normal text-muted-foreground">{summary}</span>
        )}
      </h3>
      {children}
    </section>
  );
}

/** Small heading inside a section, with an optional trailing action such as "Add". */
export function SettingsGroupHeader({
  title,
  id,
  action,
}: {
  title: string;
  id?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex min-h-8 flex-wrap items-center justify-between gap-2">
      <h3 id={id} className="text-sm font-medium">
        {title}
      </h3>
      {action}
    </div>
  );
}

/** Float as a rounded, elevated card above the scrolling content; PageContainer's padding keeps it clear of the pane edges. */
export function SettingsActionBar({
  notices,
  children,
}: {
  notices?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      data-slot="settings.actions"
      className="-mb-3 sticky bottom-4 z-10 mx-auto flex w-fit max-w-full flex-wrap items-center gap-3 rounded-xl border border-border bg-card/95 px-4 py-3 shadow-e4 backdrop-blur supports-[backdrop-filter]:bg-card/80"
    >
      {notices ? (
        <div className="min-w-0 max-w-sm space-y-1 text-sm">{notices}</div>
      ) : null}
      <div className="flex flex-wrap gap-3">{children}</div>
    </div>
  );
}
