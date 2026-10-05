import * as React from "react";
import * as Primitive from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";

// Encode every value, not just "", so application values cannot collide with
// Radix's reserved empty placeholder. FormData still receives the original value.
const encode = (value: string) => `value:${value}`;

const DismissKeyContext = React.createContext<string | null>(null);

/** Dismiss portaled listboxes on view changes without resetting their field values. */
export function SelectDismissScope({
  dismissKey,
  children,
}: {
  dismissKey: string;
  children: React.ReactNode;
}) {
  return (
    <DismissKeyContext.Provider value={dismissKey}>
      {children}
    </DismissKeyContext.Provider>
  );
}

type SelectProps = Omit<
  React.ComponentPropsWithoutRef<typeof Primitive.Trigger>,
  "value" | "defaultValue" | "onChange" | "children"
> & {
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  children: React.ReactNode;
  name?: string;
  required?: boolean;
};

export const Select = React.forwardRef<HTMLButtonElement, SelectProps>(
  (
    {
      value, defaultValue = "", onValueChange, children, name, required,
      disabled, className, form, ...props
    },
    ref,
  ) => {
    const [internalValue, setInternalValue] = React.useState(defaultValue);
    const [open, setOpen] = React.useState(false);
    const triggerRef = React.useRef<HTMLButtonElement>(null);
    const tabDirection = React.useRef(0);
    const dismissKey = React.useContext(DismissKeyContext);
    React.useEffect(() => {
      setOpen(false);
      tabDirection.current = 0;
    }, [dismissKey]);
    const current = value ?? internalValue;
    return (
      <Primitive.Root
        open={open}
        onOpenChange={setOpen}
        value={encode(current)}
        onValueChange={(encoded) => {
          const next = encoded.slice("value:".length);
          setInternalValue(next);
          onValueChange?.(next);
        }}
        disabled={disabled}
        required={required}
      >
        {name && (
          <input type="hidden" name={name} value={current} disabled={disabled} form={form} />
        )}
        <Primitive.Trigger
          {...props}
          ref={(element) => {
            triggerRef.current = element;
            if (typeof ref === "function") ref(element);
            else if (ref) ref.current = element;
          }}
          disabled={disabled}
          form={form}
          className={cn(
            "flex h-9 w-full min-w-0 items-center justify-between gap-2 rounded-md border border-input bg-background px-3 py-1 text-left text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 [&>span:first-child]:truncate",
            className,
          )}
        >
          <Primitive.Value />
          <Primitive.Icon asChild>
            <ChevronDown className="h-4 w-4 shrink-0 opacity-60" />
          </Primitive.Icon>
        </Primitive.Trigger>
        <Primitive.Portal>
          <Primitive.Content
            position="popper"
            sideOffset={4}
            collisionPadding={8}
            onKeyDown={(event) => {
              if (event.key === "Tab") {
                event.preventDefault();
                tabDirection.current = event.shiftKey ? -1 : 1;
                setOpen(false);
              }
            }}
            onCloseAutoFocus={(event) => {
              if (!tabDirection.current) return;
              event.preventDefault();
              // Radix traps Tab while open. On dismissal restore normal form
              // navigation, excluding hidden/disabled controls and focus guards.
              const controls = Array.from(
                document.querySelectorAll<HTMLElement>(
                  'a[href], button, input, select, textarea, [tabindex]',
                ),
              ).filter(
                (element) =>
                  element.tabIndex >= 0 &&
                  !element.matches(":disabled") &&
                  element.getClientRects().length > 0 &&
                  !element.hasAttribute("data-radix-focus-guard"),
              );
              const index = controls.indexOf(triggerRef.current!);
              (controls[index + tabDirection.current] ?? triggerRef.current)?.focus();
              tabDirection.current = 0;
            }}
            className="z-[100] max-h-[min(20rem,var(--radix-select-content-available-height))] w-[var(--radix-select-trigger-width)] max-w-[calc(100vw-1rem)] overflow-hidden rounded-md border bg-background text-foreground shadow-lg"
          >
            <Primitive.ScrollUpButton className="flex justify-center py-1">
              <ChevronUp className="h-4 w-4" />
            </Primitive.ScrollUpButton>
            <Primitive.Viewport className="p-1">{children}</Primitive.Viewport>
            <Primitive.ScrollDownButton className="flex justify-center py-1">
              <ChevronDown className="h-4 w-4" />
            </Primitive.ScrollDownButton>
          </Primitive.Content>
        </Primitive.Portal>
      </Primitive.Root>
    );
  },
);
Select.displayName = "Select";

export const SelectItem = React.forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof Primitive.Item>
>(({ value, children, className, ...props }, ref) => (
  <Primitive.Item
    {...props}
    ref={ref}
    value={encode(value)}
    className={cn(
      "relative flex cursor-default select-none items-center rounded-sm py-2 pl-8 pr-2 text-sm outline-none [overflow-wrap:anywhere] data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
      className,
    )}
  >
    <Primitive.ItemIndicator className="absolute left-2">
      <Check className="h-4 w-4" />
    </Primitive.ItemIndicator>
    <Primitive.ItemText>{children}</Primitive.ItemText>
  </Primitive.Item>
));
SelectItem.displayName = "SelectItem";
