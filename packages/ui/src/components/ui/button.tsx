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
        // Inverted key: hover steps the face toward neutral gray so it reads the
        // same as the surface states. That direction is lighter in the light
        // theme and darker in the dark theme, which is what --primary-hover
        // encodes per theme; keep both values opaque.
        default:
          "bg-primary text-primary-foreground hover:bg-primary-hover active:bg-primary-hover "
          + "shadow-raised-invert hover:shadow-raised-invert-hover active:shadow-pressed-invert",
        destructive:
          "bg-destructive text-destructive-foreground hover:bg-destructive/90 "
          + "shadow-raised-invert hover:shadow-raised-invert-hover active:shadow-pressed-invert",
        // Same physical key in both themes, described only by shadow: a top
        // highlight plus soft shadows on both sides of the edge, because a
        // highlight alone vanishes on white and the key fuses with the page.
        // The face stays opaque; hover never re-alphaes it.
        outline:
          "bg-card text-foreground hover:bg-card-hover hover:text-foreground "
          + "shadow-raised hover:shadow-raised-hover active:shadow-pressed active:text-foreground",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-secondary-hover hover:text-secondary-foreground "
          + "shadow-raised hover:shadow-raised-hover active:shadow-pressed",
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
