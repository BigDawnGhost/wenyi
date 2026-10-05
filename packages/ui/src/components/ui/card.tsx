import * as React from "react";
import { cn } from "@/lib/utils";

export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => (
    <div data-slot="card" ref={ref} className={cn("rounded-lg border bg-card text-card-foreground shadow-sm", className)} {...p} />
  )
);
Card.displayName = "Card";
export const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => <div data-slot="card.header" ref={ref} className={cn("flex flex-col space-y-1.5 p-6", className)} {...p} />
);
CardHeader.displayName = "CardHeader";
export const CardTitle = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => <div data-slot="card.title" ref={ref} className={cn("font-semibold leading-none tracking-tight", className)} {...p} />
);
CardTitle.displayName = "CardTitle";
export const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...p }, ref) => <div data-slot="card.content" ref={ref} className={cn("p-6 pt-0", className)} {...p} />
);
CardContent.displayName = "CardContent";
