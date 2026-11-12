import * as React from "react";
import { Eye, EyeOff } from "lucide-react";
import { cn } from "../../lib/utils";
import { Input } from "./form";

type PasswordInputProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> & {
  /** Accessible names for the toggle; the visible control is an icon only. */
  showLabel: string;
  hideLabel: string;
  /** Called before the value is first revealed, e.g. to load a saved secret. */
  onReveal?: () => void;
};

/** Secret input masked with dots by default, with an eye button inside the field. */
export const PasswordInput = React.forwardRef<HTMLInputElement, PasswordInputProps>(
  ({ className, showLabel, hideLabel, onReveal, disabled, ...props }, ref) => {
    const [visible, setVisible] = React.useState(false);
    const label = visible ? hideLabel : showLabel;
    return (
      <div className="relative min-w-0">
        <Input
          ref={ref}
          type={visible ? "text" : "password"}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          className={cn("pr-10", className)}
          {...props}
        />
        <button
          type="button"
          disabled={disabled}
          aria-label={label}
          aria-pressed={visible}
          title={label}
          onClick={() => {
            if (!visible) onReveal?.();
            setVisible((value) => !value);
          }}
          className="absolute inset-y-0 right-0 flex w-9 items-center justify-center rounded-r-md text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
        >
          {visible
            ? <EyeOff className="h-4 w-4" aria-hidden="true" />
            : <Eye className="h-4 w-4" aria-hidden="true" />}
        </button>
      </div>
    );
  },
);
PasswordInput.displayName = "PasswordInput";
