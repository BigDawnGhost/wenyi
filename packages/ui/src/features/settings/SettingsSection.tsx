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
        "min-w-0 scroll-mt-4 border-t py-6 first-of-type:border-t-0 first-of-type:pt-0",
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
    <section aria-labelledby={headingId} className="min-w-0 space-y-4 border-t pt-4">
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

/** Cancel PageContainer's edge padding so scrolling to the end cannot lift the sticky bar. */
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
      className="sticky bottom-0 z-10 -mx-6 -mb-6 flex flex-wrap items-center gap-3 border-t bg-background/95 px-6 py-3 backdrop-blur supports-[backdrop-filter]:bg-background/80"
    >
      <div className="min-w-0 flex-1 basis-48 space-y-1 text-sm">{notices}</div>
      <div className="flex flex-wrap gap-3">{children}</div>
    </div>
  );
}
