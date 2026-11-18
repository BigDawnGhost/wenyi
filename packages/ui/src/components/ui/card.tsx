import * as React from "react";
import { cn } from "../../lib/utils";

/** Group-card surface, shared by <Card> and by elements that need their own
 *  semantics (a <section> with aria-labelledby, a <p role="status">). */
export const cardSurface =
  "rounded-xl border border-border bg-card text-card-foreground transition-[transform,box-shadow] duration-motion-normal ease-standard";

export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => (
    <div data-slot="card" ref={ref} className={cn(cardSurface, className)} {...p} />
  )
);
Card.displayName = "Card";
export const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => (
    // p-4, not p-7: every page that draws a card overrides the padding to 16px
    // anyway, so the default only ever reached the rare caller that did not — and
    // those were the pages whose cards looked out of step with the rest.
    <div data-slot="card.header" ref={ref} className={cn("flex flex-col gap-1.5 p-4", className)} {...p} />
  )
);
CardHeader.displayName = "CardHeader";
export const CardTitle = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => (
    // The title is 16px tall while a row header typically also holds a 32px
    // control, so centring it inside that row leaves 8px of space above and
    // below the text that reads as padding the card does not actually have.
    // Give it the height of the row instead and centre the text inside.
    // Weight matches the h2 that heads a section elsewhere in the app: one
    // weight for "name of this group", so a card title and a page-level group
    // heading do not disagree.
    <div
      data-slot="card.title"
      ref={ref}
      className={cn("flex min-h-8 items-center font-medium leading-none tracking-tight", className)}
      {...p}
    />
  )
);
CardTitle.displayName = "CardTitle";
export const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => <div data-slot="card.content" ref={ref} className={cn("p-4 pt-0", className)} {...p} />
);
CardContent.displayName = "CardContent";
