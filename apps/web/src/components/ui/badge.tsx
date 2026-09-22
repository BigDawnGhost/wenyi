import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex shrink-0 items-center whitespace-nowrap rounded-md border px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        destructive:
          "border-transparent bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-300",
        outline: "text-foreground",
        success:
          "border-transparent bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300",
        warning:
          "border-transparent bg-amber-50 text-amber-800 dark:bg-amber-500/10 dark:text-amber-300",
        info: "border-transparent bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...p }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...p} />;
}
