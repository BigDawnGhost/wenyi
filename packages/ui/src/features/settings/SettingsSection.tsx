import { useId, type ReactNode } from "react";
import { cn } from "../../lib/utils";
import { Card } from "../../components/ui/card";

export { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";

const SETTINGS_HEADING_CLASSNAME = "text-sm font-medium";

/** Sections provide a panel by default; grouped sections own their child panels. */
export function SettingsSection({
  title,
  id,
  children,
  className,
  grouped = false,
  ...rest
}: {
  title: string;
  id?: string;
  children: ReactNode;
  className?: string;
  grouped?: boolean;
  "data-testid"?: string;
}) {
  const headingId = useId();
  const Content = grouped ? "div" : Card;
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
      <h2 id={headingId} className={cn("mb-4", SETTINGS_HEADING_CLASSNAME)}>
        {title}
      </h2>
      <Content className={cn("min-w-0 space-y-4", !grouped && "p-4")}>
        {children}
      </Content>
    </section>
  );
}

/** Global and project settings show groups inline without nested cards or folding. */
export function SettingsGroup({
  title,
  meta,
  children,
}: {
  title: string;
  /** Identity metadata (for example a license badge) read as part of the heading. */
  meta?: ReactNode;
  children: ReactNode;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="min-w-0 space-y-4">
      <h3
        id={headingId}
        className={cn(SETTINGS_HEADING_CLASSNAME, meta != null && "flex flex-wrap items-center gap-2")}
      >
        {title}
        {meta}
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
      <h3 id={id} className={SETTINGS_HEADING_CLASSNAME}>
        {title}
      </h3>
      {action}
    </div>
  );
}
