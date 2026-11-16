import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium "
    + "transition-[transform,box-shadow,background-color,color] duration-motion-fast ease-standard "
    + "focus-visible:outline-none motion-safe:active:scale-[0.98] active:duration-75 "
    + "disabled:pointer-events-none disabled:opacity-50 disabled:shadow-none",
  {
    variants: {
      variant: {
        default:
          "bg-gradient-to-b from-primary to-primary/90 text-primary-foreground "
          + "shadow-[0_1px_2px_rgba(24,26,32,0.20)] hover:shadow-[0_2px_6px_rgba(24,26,32,0.25)]",
        destructive:
          "bg-destructive text-destructive-foreground hover:bg-destructive/90 "
          + "shadow-[0_1px_2px_rgba(24,26,32,0.15)]",
        outline:
          "bg-card text-foreground shadow-e2 hover:bg-hover hover:text-foreground "
          + "hover:shadow-e3",
        secondary: "bg-secondary text-secondary-foreground hover:bg-secondary/80 shadow-none",
        ghost: "hover:bg-hover hover:text-foreground shadow-none",
        link: "text-primary underline-offset-4 hover:underline shadow-none",
      },
      size: {
        default: "h-9 px-5 py-2",
        sm: "h-8 rounded-lg px-3.5 text-xs",
        lg: "h-10 rounded-lg px-8",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button data-slot="button" ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  )
);
Button.displayName = "Button";
export { buttonVariants };
