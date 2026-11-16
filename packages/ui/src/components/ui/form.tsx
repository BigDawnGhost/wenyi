import * as React from "react";
import { cn } from "../../lib/utils";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...p }, ref) => (
    <input ref={ref} className={cn("flex h-9 w-full rounded-lg bg-background px-3.5 py-1 text-sm shadow-e0 input-quiet placeholder:text-placeholder transition-[box-shadow] duration-motion-fast ease-standard focus-visible:outline-none disabled:opacity-50 disabled:shadow-none", className)} {...p} />
  )
);
Input.displayName = "Input";

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...p }, ref) => (
    <textarea ref={ref} className={cn("flex min-h-20 w-full rounded-lg bg-background px-3.5 py-2 text-sm shadow-e0 input-quiet placeholder:text-placeholder transition-[box-shadow] duration-motion-fast ease-standard focus-visible:outline-none disabled:opacity-50 disabled:shadow-none", className)} {...p} />
  )
);
Textarea.displayName = "Textarea";

export const Label = React.forwardRef<HTMLLabelElement, React.LabelHTMLAttributes<HTMLLabelElement>>(
  ({ className, ...p }, ref) => (
    <label ref={ref} className={cn("text-sm font-medium leading-none peer-disabled:opacity-70", className)} {...p} />
  )
);
Label.displayName = "Label";

type SwitchProps = Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children" | "onClick"> & {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
};

export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  ({ checked, onCheckedChange, className, ...p }, ref) => (
    <button
      {...p}
      ref={ref}
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-primary" : "bg-input",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "block h-4 w-4 rounded-full bg-background shadow-[0_1px_2px_rgba(24,26,32,0.25)] transition-transform",
          checked ? "translate-x-4" : "translate-x-0",
        )}
      />
    </button>
  ),
);
Switch.displayName = "Switch";
