import { useId, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
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
      <h3 id={headingId} className={SETTINGS_HEADING_CLASSNAME}>
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
      <h3 id={id} className={SETTINGS_HEADING_CLASSNAME}>
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
    /* Positioning and the entrance share no property.

       The wrapper spans its container with the plain box model (left:0 and
       right:0), and the bar inside it is a flex child, so the bar is centred
       by layout: its own width never feeds back into its position and a
       change of width grows from the centre. No measured value is stored, so
       there is nothing that can be stale for a frame while a route settles.

       The entrance animates only the inner element and only opacity and
       transform, which no other rule in this component writes. */
    <div
      data-slot="settings.actions"
      style={{
        position: "fixed",
        bottom: 16,
        // Inset from the sidebar edge so the flex child centres on the content
        // pane. left is used rather than a margin so the box keeps a right edge
        // of its own: a margin plus left:0/right:0 leaves the width to two
        // competing resolvers and the shell can briefly collapse.
        left: "var(--sidebar-w, 240px)",
        right: 0,
        // The sidebar animates its own width while --sidebar-w switches in one
        // step, so without a matching transition here the bar jumps to the new
        // inset while the pane is still sliding underneath it. Same duration and
        // curve as the sidebar, so the two stay locked together.
        // 280ms cubic-bezier(0.22, 1, 0.36, 1): the sidebar's own duration-sidebar
        // and ease-sidebar values, spelled out because this is an inline style.
        transition: "left 280ms cubic-bezier(0.22, 1, 0.36, 1)",
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
        zIndex: 10,
      }}
    >
      <div
        className="glass-bar flex flex-wrap items-center justify-center gap-3 rounded-3xl px-4 py-3"
        style={{
          pointerEvents: "auto",
          // The frost layer is an absolutely positioned ::before with inset:0
          // and anchors to the nearest positioned ancestor.
          position: "relative",
          width: "max-content",
          maxWidth: "calc(100% - 32px)",
          animation: "bar-enter 220ms cubic-bezier(0.05, 0.7, 0.1, 1) backwards",
        }}
      >
        {notices ? (
          <div className="min-w-0 max-w-sm space-y-1 text-sm">{notices}</div>
        ) : null}
        <div className="flex flex-wrap gap-3">{children}</div>
      </div>
    </div>
  );
}
